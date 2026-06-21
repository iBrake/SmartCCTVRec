import socket
import threading
import json
import configparser
import os
import subprocess
from datetime import datetime
import time
import email.encoders
import platform
import os
import signal
import sys
import traceback
import shutil

from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email.mime.image import MIMEImage
from email import encoders
from subprocess import Popen, PIPE

# A thread-safe way to keep track of which cameras are currently recording.
# We use a dictionary to store device names to prevent re-triggering.
currently_recording = {}
recording_lock = threading.Lock()
post_buffer = 0;
seen_device_names = set()
seen_devices_lock = threading.Lock()
buffer_processes = {}
buffer_lock = threading.Lock()

#Below adds lines for outputting to a dedicated log, since we're using the journal, this isn't needed.
#_print=print
#def print(*args, **kw):
#    _print("[%s]" % (datetime.now().strftime("%y/%m/%d %H:%M:%S")),*args, **kw)

def cleanup_stale_buffers():
    """
    Scans the RAM disk (/dev/shm) for any old buffer directories
    from a previous run and deletes them.
    """
    print("🧹 Cleaning up stale buffers from previous runs...")
    ram_disk = "/dev/shm"
    try:
        for item in os.listdir(ram_disk):
            item_path = os.path.join(ram_disk, item)
            # Check if it's a directory and matches our naming convention
            # (rolling buffers and per-event pre-event snapshots).
            if os.path.isdir(item_path) and item.startswith(("cctv_buffer_", "cctv_snap_")):
                print(f"🚮 Removing stale buffer directory: {item_path}")
                # shutil.rmtree() deletes a directory and all its contents
                shutil.rmtree(item_path)
        print("✅ Buffer cleanup complete.")
    except Exception as e:
        print(f"⚠️ Warning: Could not clean up stale buffers. {e}")

def free_port(port):
    #Finds and kills the process that is currently using the specified port.
    #This is a cross-platform function for Windows, Linux, and macOS.
    print(f"💪 Attempting to free port {port}...")
    system = platform.system()

    try:
        if system == "Windows":
            # Command to find the PID using the port
            find_pid_cmd = f"netstat -ano | findstr :{port}"
            result = subprocess.run(find_pid_cmd, shell=True, capture_output=True, text=True)
            output = result.stdout.strip()

            if not output:
                print(f"✅ Port {port} is already free.")
                return

            # The PID is the last column in the output
            lines = output.split('\n')
            for line in lines:
                if "LISTENING" in line:
                    pid = line.split()[-1]
                    # Command to kill the process
                    kill_cmd = f"taskkill /F /PID {pid}"
                    print(f"🗡️ Found process {pid} on port {port}. Attempting to kill...")
                    subprocess.run(kill_cmd, shell=True, capture_output=True)
                    print(f"✅ Process {pid} killed. Wait 5 seconds")
                    time.sleep(5)
                    return # Assume first listening process is the one

        elif system in ["Linux", "Darwin"]: # Darwin is the system name for macOS
            # Command to find the PID. The -t flag gives only the PID.
            find_pid_cmd = f"lsof -t -i:{port}"
            result = subprocess.run(find_pid_cmd, shell=True, capture_output=True, text=True)
            pid_str = result.stdout.strip()

            if not pid_str:
                print(f"✅ Port {port} is already free.")
                return

            pid = int(pid_str)
            print(f"🔪 Found process {pid} on port {port}. Attempting to kill...")
            # Use os.kill for a more direct way to terminate on Unix-like systems
            os.kill(pid, signal.SIGKILL)
            print(f"✅ Process {pid} killed. Wait 5 seconds")
            time.sleep(5)

        else:
            print(f"⚠️ Unsupported OS '{system}'. Cannot free port automatically.")

    except (subprocess.CalledProcessError, PermissionError, ValueError) as e:
        print(f"❌ Error while trying to free port {port}: {e}")

def apply_overlay_to_buffer(buffer_segments, config, device_name):
    """
    Re-encodes a list of video segments to add a text overlay.
    This is CPU-intensive.
    Returns a list of paths to the new, encoded segment files.
    """
    overlay_text = config.get('recording', 'pre_event_overlay', fallback=None)
    if not overlay_text:
        return buffer_segments # Return the original list if no overlay is set

    print(f"{device_name}: Applying '{overlay_text}' overlay to pre-event buffer...")

    # Create a new directory in RAM to store the encoded segments
    encoded_buffer_dir = f"/dev/shm/cctv_buffer_{device_name}_encoded"
    os.makedirs(encoded_buffer_dir, exist_ok=True)

    encoded_segments = []

    for i, segment_path in enumerate(buffer_segments):
        output_path = os.path.join(encoded_buffer_dir, f"encoded_segment_{i:03d}.ts")

        """
        FFMPEG font location logic:
        fontsize=h/30: This makes the font size dynamic.
        h: variable representing the video's total height in pixels. For a 720p video, the font size would be 720 / 30 = 24.
        y=h-th-10: This positions the text at the bottom. h is the video height. th is a variable for the text's height
        h-th: calculates the y coordinate to place the bottom of the text at the bottom of the video.
        h-th-10: subtracts an extra 10 pixels to give you a nice, clean 10-pixel padding from the bottom edge.
        x=10: fixed 10 pixel padding.
        """

        try:
            ffmpeg_command = [
                'ffmpeg',
                '-i', segment_path,
                # Re-encode video to apply the filter
                '-c:v', 'libx264',
                '-preset', 'veryfast', # Fast encoding preset
                '-crf', '23',         # Decent quality
                '-vf', f"drawtext=text='{overlay_text}':fontcolor=white:fontsize=h/15:box=1:boxcolor=black@0.5:x=10:y=h-th-10",
                # Copy the audio stream without re-encoding
                '-c:a', 'copy',
                '-y',
                output_path
            ]
            subprocess.run(ffmpeg_command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            encoded_segments.append(output_path)
        except Exception as e:
            print(f"❌ FAILED to apply overlay to segment {segment_path}. Error: {e}")
            # If one fails, we should probably stop and use the original buffer
            return buffer_segments

    #print(f"✅ Overlay applied to {len(encoded_segments)} segments.")
    return encoded_segments


def start_rolling_buffer(ip_address, config, device_name):
    """
    Starts and monitors an FFmpeg rolling buffer process.
    If the process ever stops, this function will automatically restart it.
    """
    global buffer_processes

    # This outer loop ensures the buffer process "self-heals" and restarts if it ever dies.
    while True:
        try:
            # --- All original setup code is now inside the loop ---
            pre_event_seconds = config.getint('recording', 'pre_event_record_seconds')
            rtsp_template = config.get('recording', 'rtsp_template')

            buffer_dir = f"/dev/shm/cctv_buffer_{device_name}"
            os.makedirs(buffer_dir, exist_ok=True)

            playlist = os.path.join(buffer_dir, 'buffer.m3u8')
            rtsp_url = rtsp_template.format(ip=ip_address)

            print(f"{device_name}: Started pre-event rolling buffer.")

            ffmpeg_command = [
                'ffmpeg',
                '-loglevel', 'fatal',
                '-rtsp_transport', 'tcp',
                '-i', rtsp_url,
                '-c:v', 'copy',
                '-c:a', 'aac',      # <-- RE-ENCODE audio to AAC
                '-b:a', '48k',      # <-- Set a good bitrate
                '-ac', '1',         # <-- Set mono
                '-f', 'segment',
                '-segment_time', str(1),
                '-segment_list', playlist,
                '-segment_wrap', str(pre_event_seconds),
                '-reset_timestamps', '1',
                '-y',
                os.path.join(buffer_dir, 'stream-%03d.ts')
            ]

            process = subprocess.Popen(ffmpeg_command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

            with buffer_lock:
                buffer_processes[device_name] = process

            # --- Monitoring loop ---
            # This loop will run as long as FFmpeg is alive.
            # When FFmpeg crashes or stops, iter() will end, and the loop will break.
            for line in iter(process.stderr.readline, b''):
                print(f"FFmpeg Buffer ({device_name}): {line.decode('utf-8').strip()}", flush=True)

        except Exception as e:
            # This catches errors in *starting* FFmpeg (e.g., bad config)
            print(f"❌ CRITICAL ERROR in rolling buffer thread for {device_name}: {e}", flush=True)

        finally:
            # --- This block now runs when FFmpeg stops ---
            print(f"{device_name}: Rolling buffer has stopped. Restarting in 1s.")
            with buffer_lock:
                if device_name in buffer_processes:
                    del buffer_processes[device_name]

            # Wait 1 second before restarting to prevent a rapid crash loop
            time.sleep(1)

def extract_snapshot(video_path, output_image_path, width):
    """
    Extracts a single frame from the beginning of a video file using FFmpeg.
    Returns the path to the image on success, None on failure.
    """
    try:
        #print(f"📸 Extracting snapshot from '{os.path.basename(video_path)}'...")
        # -ss 00:00:01: Seek 1 second in (safer than the very first frame)
        # -vframes 1: Grab only one frame
        # -vf scale={width}:-1: Resize the frame to the specified width, maintaining aspect ratio
        ffmpeg_command = [
            'ffmpeg', '-i', video_path,
            '-ss', '00:00:01',
            '-vframes', '1',
            '-vf', f'scale={width}:-1',
            '-y', output_image_path
        ]
        subprocess.run(ffmpeg_command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        #print("✅ Snapshot created successfully.")
        return output_image_path
    except Exception as e:
        print(f"❌ FAILED to extract snapshot. Error: {e}")
        return None

def reencode_video(video_path, output_video_path, res):
    """
    Re-encodes a video to a smaller size using FFmpeg.
    Returns the path to the new video on success, None on failure.
    """
    config = load_config()
    bitrate = config.get('attachments', 'vid_bitrate', fallback='2M')
    try:
        #print(f"🎬 Re-encoding video for attachment to {res}p...")
        # -c:v libx264: Use the standard H.264 video codec
        # -preset veryfast: Good balance of speed and file size
        # -crf 23: Good quality setting (lower is better, 23 is a sane default)
        # -vf scale=-2:{res}: Scale to the target height (720 or 1080)

        ffmpeg_command = [
            'ffmpeg', '-i', video_path,
            '-c:v', 'libx264',
            '-preset', 'fast',
            '-b:v', bitrate,
            '-maxrate', bitrate,
            '-minrate', bitrate,
            '-bufsize', '4M',
            '-vf', f'scale=-2:{res}',
            '-af', 'aresample=48000,acompressor=threshold=-21dB:ratio=4:makeup=2,loudnorm=I=-16:LRA=7:tp=-1.5',
            '-c:a', 'aac',
            '-ac', '1', #mono
            '-b:a', '32k',
            '-y', output_video_path
        ]
        subprocess.run(ffmpeg_command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        #print("✅ Video re-encoded successfully.")
        return output_video_path
    except Exception as e:
        print(f"❌ FAILED to re-encode video. Error: {e}")
        return None

def get_video_duration(video_path):
    """
    Returns the duration of a video file in seconds (float), or None on failure.
    Uses ffprobe. Needed by the 2-pass encoder to work out the target bitrate.
    """
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'error',
             '-show_entries', 'format=duration',
             '-of', 'default=noprint_wrappers=1:nokey=1', video_path],
            capture_output=True, text=True, check=True
        )
        return float(result.stdout.strip())
    except Exception as e:
        print(f"❌ FAILED to probe video duration. Error: {e}")
        return None

def reencode_video_2pass(video_path, output_video_path, res, target_size_mb):
    """
    Re-encodes a video to (approximately) a target file size using 2-pass VBR x264.
    This is the fallback for when a normal re-encode still comes out bigger than
    'vid_max_size', which would otherwise make sendmail reject the message.

    The video bitrate is derived from the target size and the clip's duration:
        total_bitrate = target_bits / duration
        video_bitrate = total_bitrate - audio_bitrate
    A small headroom factor keeps the result under the cap once container/muxing
    overhead is accounted for.

    Returns the path to the new video on success, None on failure.
    """
    audio_bitrate_k = 32  # keep in sync with reencode_video's '-b:a'

    duration = get_video_duration(video_path)
    if not duration or duration <= 0:
        print("❌ Cannot 2-pass encode: unknown or zero duration.")
        return None

    # Leave ~6% headroom for container/muxing overhead so we land *under* the cap.
    target_bits = target_size_mb * 1024 * 1024 * 8 * 0.94
    total_bitrate = target_bits / duration                 # bits/sec
    video_bitrate = total_bitrate - (audio_bitrate_k * 1000)

    if video_bitrate <= 0:
        print(f"❌ Target {target_size_mb}MB too small for a {duration:.0f}s clip at {audio_bitrate_k}k audio.")
        return None

    video_bitrate_k = int(video_bitrate / 1000)
    print(f"📉 2-pass VBR: targeting {target_size_mb}MB -> ~{video_bitrate_k}k video over {duration:.0f}s.")

    # x264 2-pass writes a stats file between passes; keep it beside the output.
    passlog = output_video_path + "-2pass"

    common_video = [
        '-c:v', 'libx264',
        '-preset', 'medium',
        '-b:v', f'{video_bitrate_k}k',
        '-vf', f'scale=-2:{res}',
    ]

    try:
        # --- Pass 1: analyse only. No audio, no real output file. ---
        pass1 = [
            'ffmpeg', '-y', '-i', video_path,
            *common_video,
            '-pass', '1', '-passlogfile', passlog,
            '-an', '-f', 'mp4', os.devnull
        ]
        subprocess.run(pass1, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # --- Pass 2: the real encode, with audio (same chain as reencode_video). ---
        pass2 = [
            'ffmpeg', '-y', '-i', video_path,
            *common_video,
            '-pass', '2', '-passlogfile', passlog,
            '-af', 'aresample=48000,acompressor=threshold=-21dB:ratio=4:makeup=2,loudnorm=I=-16:LRA=7:tp=-1.5',
            '-c:a', 'aac',
            '-ac', '1',  # mono
            '-b:a', f'{audio_bitrate_k}k',
            output_video_path
        ]
        subprocess.run(pass2, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        return output_video_path
    except Exception as e:
        print(f"❌ FAILED 2-pass re-encode. Error: {e}")
        return None
    finally:
        # Clean up x264's stats files (ffmpeg appends '-0.log' / '-0.log.mbtree').
        for suffix in ['-0.log', '-0.log.mbtree', '.log', '.log.mbtree']:
            try:
                stats = passlog + suffix
                if os.path.exists(stats):
                    os.remove(stats)
            except Exception:
                pass

def is_within_alert_window():
    """
    Checks if the current time is within the email alert window specified in the config.
    Handles overnight time windows correctly (e.g., 22:00 to 06:00).
    """
    config = load_config()
    try:
        if not config.getboolean('email_alerts', 'enabled'):
            return False

        start_str = config.get('email_alerts', 'email_alert_time_start')
        stop_str = config.get('email_alerts', 'email_alert_time_stop')

        start_time = datetime.strptime(start_str, '%H:%M').time()
        stop_time = datetime.strptime(stop_str, '%H:%M').time()
        now_time = datetime.now().time()

        # Logic to handle overnight window
        if start_time > stop_time:
            return now_time >= start_time or now_time <= stop_time
        # Logic for a standard same-day window
        else:
            return start_time <= now_time <= stop_time

    except (configparser.NoOptionError, ValueError) as e:
        print(f"⚠️ Email alert time check failed. Is conf.ini configured correctly? Error: {e}")
        return False

def is_email_zone(device_name):
    """
    Returns True if this camera/zone is allowed to generate an alarm email.

    Controlled by conf.ini:

        [Zones_for_alarm_email]
        zones = FrontDoor,BackDoor

    If the section/key is missing or left blank, ALL zones are allowed - the
    filter is opt-in, so existing setups behave exactly as before. Matching
    ignores surrounding whitespace and is case-insensitive, so
    "FrontDoor, backdoor" still matches the device named "FrontDoor".
    """
    config = load_config()
    raw = config.get('Zones_for_alarm_email', 'zones', fallback='').strip()
    if not raw:
        return True  # no zone filter configured -> every zone may email
    allowed = {z.strip().lower() for z in raw.split(',') if z.strip()}
    return device_name.strip().lower() in allowed

def send_alert_email(device_name, trigger_type, image_path, attachment_path=None):
    """
    Builds and sends a MIME email with an embedded image and an optional video attachment.
    Cleans up the temp files after sending.
    """
    #print(f"📧 Preparing email for '{device_name}'...")
    config = load_config()

    try:
        recipient = config.get('email_alerts', 'alert_email')
        sender = config.get('email_alerts', 'sender_email')

        msg = MIMEMultipart('related')
        msg["From"] = sender
        msg["To"] = recipient
        msg["Subject"] = f"Alert: {trigger_type} alarm trigger on {device_name} camera"

        # Create an 'alternative' container for text and HTML
        msg_alternative = MIMEMultipart('alternative')#we need a plain and HTML version or some clients won't show anything.
        msg.attach(msg_alternative)

        plain_text_body = f"Please switch to HTML view."
        msg_alternative.attach(MIMEText(plain_text_body, 'plain'))

        vidattached = False
        #check if we have a vid attachment.
        if attachment_path and os.path.exists(attachment_path):
            vidattached = True

        additionalComments = ""
        if vidattached:
            additionalComments = "Please see attached video for the full event."

        # HTML auto resize attempt...
        html_body = f"""
        <html><body>
        <p><b>Camera:</b> {device_name}<br><b>Trigger type:</b> {trigger_type}<br><b>Time:</b> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
        <p>Snapshot from the event:</p>
        <img src="cid:snapshot" style="max-width:100%; height:auto;">
        <p>{additionalComments}</p>
        </body></html>
        """
        msg_alternative.attach(MIMEText(html_body, 'html'))

        # Embed the snapshot image
        if image_path and os.path.exists(image_path):
            # Automatically get the subtype (jpg, png, etc.) from the filename
            subtype = image_path.split('.')[-1]
            with open(image_path, 'rb') as img_file:
                # Pass the file object directly, DO NOT use .read()
                img = MIMEImage(img_file.read(), _subtype=subtype)
            img.add_header('Content-ID', '<snapshot>')
            msg.attach(img)

        if attachment_path and os.path.exists(attachment_path):
            #print(f"📎 Attaching video: {os.path.basename(attachment_path)}")
            with open(attachment_path, 'rb') as attachment:
                part = MIMEBase('application', 'octet-stream')
                # Pass the file object directly to the payload, DO NOT use .read()
                part.set_payload(attachment.read())
            encoders.encode_base64(part)
            part.add_header('Content-Disposition', f'attachment; filename="{os.path.basename(attachment_path)}"')
            msg.attach(part)

        p = Popen(["/usr/sbin/sendmail", "-t", "-oi"], stdin=PIPE)
        p.communicate(msg.as_bytes())

        print(f"{device_name}: Email alert successfully passed to sendmail.")

    except Exception as e:
        print(f"❌ FAILED to build or send email. Error: {e}")

    finally:
        # --- THIS IS THE NEW CLEANUP LOGIC ---
        # This block runs whether the email succeeded or failed.
        print(f"{device_name}: Cleaning up temp email files...")
        try:
            if image_path and os.path.exists(image_path):
                os.remove(image_path)
            if attachment_path and os.path.exists(attachment_path):
                os.remove(attachment_path)
        except Exception as e:
            print(f"⚠️ Warning: Could not delete temp file. Error: {e}")
        print(f"{device_name}: All done!")

def manage_folder_size():
    """
    Checks the total size of the recording directory and deletes the oldest
    files if the size exceeds the configured limit.
    """
    global post_buffer;
    config = load_config()
    base_location = config.get('recording', 'location')
    max_size_gb = config.getfloat('housekeeping', 'max_folder_size_gb')
    threshold_mb = config.getfloat('housekeeping', 'cleanup_threshold_mb')

    # Convert configured values to bytes for accurate comparison
    max_size_bytes = max_size_gb * 1024 * 1024 * 1024
    threshold_bytes = threshold_mb * 1024 * 1024

    if not os.path.isdir(base_location):
        # The recording folder might not exist yet.
        # The recording folder might not exist yet.
        return

    # --- 1. Calculate total size and get all video files ---
    total_size = 0
    all_files = []
    # os.walk is perfect for scanning through all subdirectories
    for root, dirs, files in os.walk(base_location):
        for name in files:
            # We only want to manage video files created by this script
            if name.endswith('.mp4'):
                try:
                    file_path = os.path.join(root, name)
                    file_size = os.path.getsize(file_path)
                    total_size += file_size
                    # Store path and modification time (to find the oldest)
                    all_files.append((file_path, os.path.getmtime(file_path)))
                except FileNotFoundError:
                    # File might have been deleted by another process, just skip it
                    continue

    # --- 2. Check if cleanup is needed ---
    if total_size > (max_size_bytes - threshold_bytes):
        print(f"🧹 Housekeeping: Folder size ({total_size / 1024**3:.2f} GB) is near limit ({max_size_gb} GB). Starting cleanup.")

        # Sort files by modification time, oldest first
        all_files.sort(key=lambda f: f[1])

        # --- 3. Delete oldest files until we are under the limit ---
        while total_size > (max_size_bytes - threshold_bytes):
            if not all_files:
                print("⚠️ Housekeeping: No more files to delete, but still over size limit.")
                break

            # Get the oldest file from the top of the sorted list
            oldest_file_path, _ = all_files.pop(0)
            try:
                file_size_to_delete = os.path.getsize(oldest_file_path)
                os.remove(oldest_file_path)
                total_size -= file_size_to_delete
                #print(f"🗑️ Deleted oldest file: {os.path.basename(oldest_file_path)}")
            except OSError as e:
                print(f"❌ Housekeeping Error: Could not delete file {oldest_file_path}. Reason: {e}")

        print("✅ Housekeeping: Cleanup complete.")
    #else:
    #    print(f"Housekeeping: Folder size is OK. ({total_size / 1024**3:.2f} / {max_size_gb} GB)")

def housekeeping_daemon():
    """
    A daemon thread that runs the folder management task periodically.
    """
    print("🧹 Housekeeping thread started.")
    config = load_config()
    interval = config.getint('housekeeping', 'cleanup_interval_seconds')

    while True:
        try:
            manage_folder_size()
        except Exception as e:
            # Catch any unexpected errors to prevent the thread from crashing
            print(f"❌ CRITICAL ERROR in housekeeping thread: {e}")
        time.sleep(interval)

def clear_ram_buffer(device_name):
    """
    Deletes all files inside a camera's RAM disk buffer directory.
    This is called after an event to ensure the next event starts fresh.
    """
    buffer_dir = f"/dev/shm/cctv_buffer_{device_name}"
    if not os.path.isdir(buffer_dir):
        return # Nothing to clear

    #print(f"{device_name}: Clearing RAM buffer")
    try:
        # List all files in the directory
        for filename in os.listdir(buffer_dir):
            file_path = os.path.join(buffer_dir, filename)
            try:
                # Delete each file
                os.remove(file_path)
            except Exception as e:
                print(f"⚠️ Warning: Could not delete buffer file {file_path}: {e}")
        print(f"{device_name}: Pre-event buffer cleared.")
    except Exception as e:
        print(f"❌ FAILED to clear buffer for {ip_address}. Error: {e}")

def cleanup_event_snapshot(device_name, snapshot_dir):
    """
    Deletes the per-event pre-event snapshot directory once we're done with it.

    Replaces the old SIGSTOP/SIGCONT "pause + resume" dance. The rolling buffer
    is never frozen now (freezing left its camera connection unread, and the TCP
    backpressure stalled the camera's streamer ~1s into every event), so there is
    nothing to un-pause - the live ring is left running and full. We only need to
    bin the temporary snapshot copies. Safe to call no matter how record_video
    exits, including the NERF early-return path.
    """
    if not snapshot_dir:
        return
    try:
        if os.path.isdir(snapshot_dir):
            shutil.rmtree(snapshot_dir)
    except Exception as e:
        print(f"⚠️ Warning: Could not remove pre-event snapshot for {device_name}: {e}")


def record_video(device_name, ip_address, config, TrigType):
    #Manages a recording session and triggers post-recording actions like email alerts.

    global currently_recording, buffer_processes, buffer_lock

    # --- 1. SNAPSHOT THE PRE-EVENT BUFFER (no freeze) ---
    # We COPY the current rolling-buffer segments out to a private per-event
    # directory instead of SIGSTOP-ing the buffer ffmpeg. Freezing the buffer
    # left its camera TCP connection unread; the resulting backpressure stalled
    # the camera's (single-threaded) streamer ~1s into every event and dropped
    # frames on the live recording. Copying from /dev/shm is a RAM->RAM copy of a
    # few MB and effectively instant, with ~1s of slack before the ring would
    # overwrite these segments. The buffer keeps running and draining its socket
    # throughout (no backpressure), and now stays permanently full so a pre-roll
    # is always ready - no dead window after an event.
    snapshot_dir = None
    snapshot_segments = []          # chronologically-ordered copies for the prepend
    is_buffer_ready = False         # True once we hold a full pre-event snapshot

    try:
        pre_event_seconds = config.getint('recording', 'pre_event_record_seconds')
        buffer_dir = f"/dev/shm/cctv_buffer_{device_name}"

        if pre_event_seconds > 0 and os.path.isdir(buffer_dir):
            try:
                # Oldest-first list of the ring's segments by mtime.
                all_ts = []
                for fname in os.listdir(buffer_dir):
                    if fname.endswith('.ts'):
                        fpath = os.path.join(buffer_dir, fname)
                        try:
                            all_ts.append((fpath, os.path.getmtime(fpath)))
                        except FileNotFoundError:
                            continue   # ring overwrote it mid-scan
                all_ts.sort(key=lambda x: x[1])

                # Take the newest N segments, INCLUDING the one the buffer ffmpeg
                # is currently writing. That newest segment runs from the last
                # keyframe up to ~now (the trigger), so it is exactly what makes the
                # pre-roll abut the event recording (which itself starts on its next
                # keyframe). We deliberately do NOT drop it: "copy" can only cut on
                # keyframes, so with a ~5s GOP each segment is ~5s of video, and
                # dropping the newest punched a ~one-GOP hole right at the splice.
                # It may be copied mid-write, but TS tolerates a torn trailing
                # packet and the later concat + MP4 repackage re-muxes it cleanly.
                if len(all_ts) >= pre_event_seconds:
                    selected = all_ts[-pre_event_seconds:]
                    snapshot_dir = f"/dev/shm/cctv_snap_{device_name}_{datetime.now().strftime('%H%M%S_%f')}"
                    os.makedirs(snapshot_dir, exist_ok=True)

                    for i, (src, _) in enumerate(selected):
                        dst = os.path.join(snapshot_dir, f"snap_{i:03d}.ts")
                        try:
                            shutil.copy2(src, dst)   # copy2 keeps mtime, and the index name keeps order
                            snapshot_segments.append(dst)
                        except FileNotFoundError:
                            print(f"{device_name}: Pre-event segment vanished mid-copy, skipping.")

                    if len(snapshot_segments) >= pre_event_seconds:
                        is_buffer_ready = True
                        print(f"{device_name}: Pre-event snapshot captured ({len(snapshot_segments)} segments).")
                    else:
                        print(f"{device_name}: Pre-event snapshot incomplete ({len(snapshot_segments)}/{pre_event_seconds}). Not using.")
                else:
                    print(f"{device_name}: Pre-event not full ({len(all_ts)}/{pre_event_seconds}). Not using.")
            except Exception as e:
                print(f"❌ Error snapshotting pre-event buffer for {device_name}: {e}")
    except Exception as e:
        print(f"⚠️ Warning: Could not snapshot pre-event buffer for {device_name}. {e}")
    # -----------------------------------

    with recording_lock:
        start_time = currently_recording[device_name].get('start_time', datetime.now())
        start_time_str = start_time.strftime("%Y-%m-%d %H:%M:%S")

    base_location = config.get('recording', 'location')
    rtsp_template = config.get('recording', 'rtsp_template')
    max_duration = config.get('recording', 'max_recording_duration_seconds')
    temp_folder = os.path.join(base_location, "temp")

    rtsp_url = rtsp_template.format(ip=ip_address)
    device_folder = os.path.join(base_location, device_name)
    os.makedirs(device_folder, exist_ok=True)

    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    final_output_mp4 = os.path.join(device_folder, f"{timestamp} - {TrigType}.mp4")
    temp_event_ts = os.path.join(temp_folder, f"{timestamp} - {TrigType}.ts")

    ffmpeg_command = [
        'ffmpeg',
        '-rtsp_transport', 'tcp',
        '-i', rtsp_url,
        '-t', max_duration,
        '-c:v', 'copy', # Stream copy, low CPU
        '-c:a', 'aac',      # <-- RE-ENCODE audio to AAC
        '-b:a', '48k',      # <-- Set a good bitrate
        '-ac', '1',
        '-reset_timestamps', '1', # <-- ADDED: Match the buffer's timestamps
        '-y',
        temp_event_ts  # <-- MODIFIED: Record to the .ts file
    ]

    process = subprocess.Popen(ffmpeg_command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    with recording_lock:
        if device_name in currently_recording:
            currently_recording[device_name]['process'] = process

    try:
        while True:
            with recording_lock:
                if device_name not in currently_recording:
                    break
                if datetime.now() >= currently_recording[device_name]['end_time']:
                    break
            time.sleep(1)
    finally:
        # --- STOP THE RECORDING PROCESS ---
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()

        end_time = datetime.now()
        total_duration = round((end_time - start_time).total_seconds())
        dirAndFile = final_output_mp4.replace(device_folder,"")
        dirAndFile = device_name + dirAndFile

        print(f"{device_name}: Finished recording, {total_duration}s, File:'{dirAndFile}'.")

        #We shouldn't continue if it's a NERF
        rapid_succession_detected = False
        with recording_lock:
            if device_name in currently_recording:
                retrigger_count = currently_recording[device_name].get('retrigger_count', 1)
                rapid_succession_detected = currently_recording[device_name].get('rapid_succession_detected', False)
                del currently_recording[device_name]
        print(f"{device_name}: Now ready for new events.")

        if not rapid_succession_detected:
            path_without_ext, ext = os.path.splitext(temp_event_ts)
            new_file_path = f"{path_without_ext} NERF{ext}"
            try:
                os.rename(temp_event_ts, new_file_path)
                print(f"{device_name}: Didn't meet criteria, NERF.")
                #path_for_email = new_file_path
                if config.getboolean('recording', 'delete_nerf'):
                    os.remove(new_file_path)
            except OSError as e:
                print(f"❌ Failed to rename NERF file: {e}")
            # A NERF took a pre-event snapshot at the top but won't use it; bin it.
            # (The live buffer was never paused, so there's nothing to resume.)
            cleanup_event_snapshot(device_name, snapshot_dir)
            return

        retrigger_count = 0

        # This will hold the path to the snapshot. We create it NOW, from the event file.
        image_to_embed = None

        # --- NEW: EXTRACT SNAPSHOT (FROM THE EVENT FILE *BEFORE* PREPENDING) ---
        if os.path.exists(temp_event_ts):
            try:
                print(f"{device_name}: Extracting snapshot from event file...")
                temp_folder = os.path.join(base_location, "temp")
                os.makedirs(temp_folder, exist_ok=True)
                snapshot_width = config.getint('attachments', 'snapshot_width', fallback=640)
                snapshot_path = os.path.join(temp_folder, f"{timestamp}-snapshot.jpg")
                image_to_embed = extract_snapshot(temp_event_ts, snapshot_path, snapshot_width)
            except Exception as e:
                print(f"❌ FAILED to extract snapshot before prepending. Error: {e}")
        # --- END OF NEW SNAPSHOT LOGIC ---

        # This will point to the final file for post-processing
        final_video_path = final_output_mp4

        # This will be the file that is input to our final conversion step
        input_for_repackaging = temp_event_ts

        if is_buffer_ready and snapshot_segments and os.path.exists(temp_event_ts):
            print(f"{device_name}: Adding pre-event buffer..")

            concat_list_path = os.path.join(snapshot_dir, 'concat_list.txt')
            combined_ts_file = f"{os.path.splitext(temp_event_ts)[0]}-COMBINED.ts"

            try:
                # --- A. Pre-event segments come from the snapshot taken at the top
                #        of this function. They were copied out of the live ring at
                #        trigger time and are already in chronological order, so the
                #        still-running buffer can't overwrite them under us. ---
                segment_files = list(snapshot_segments)

                # --- B. Check for and apply overlay ---
                segment_files_for_concat = apply_overlay_to_buffer(segment_files, config, device_name)

                event_file_for_concat = temp_event_ts

                # --- C. CHECK FOR CODEC MISMATCH ---
                if segment_files_for_concat is not segment_files:
                    # We must re-encode the main event file to match the buffer's new codec
                    print(f"{device_name}: Re-encoding event file to match overlay codec...")
                    reencoded_event_ts = os.path.join(device_folder, "temp_event_reencoded.ts")
                    ffmpeg_command = [
                        'ffmpeg', '-i', temp_event_ts,
                        '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '23',
                        '-c:a', 'copy',
                        '-y', reencoded_event_ts
                    ]
                    subprocess.run(ffmpeg_command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    event_file_for_concat = reencoded_event_ts

                # --- D. Create the concat_list.txt file (FIXED) ---
                with open(concat_list_path, 'w') as f:
                    for segment in segment_files_for_concat:
                        f.write(f"file '{segment}'\n")
                    f.write(f"file '{event_file_for_concat}'\n")

                # --- E. Run FFmpeg concat demuxer ---
                ffmpeg_command = [
                    'ffmpeg',
                    '-f', 'concat',
                    '-safe', '0',
                    '-i', concat_list_path,
                    '-c', 'copy',
                    '-y',
                    combined_ts_file
                ]
                subprocess.run(ffmpeg_command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

                print(f"{device_name}: Buffer prepended successfully.")
                input_for_repackaging = combined_ts_file

                # --- F. Clean up intermediate files ---
                os.remove(temp_event_ts)
                if event_file_for_concat != temp_event_ts:
                    os.remove(event_file_for_concat)
                # concat_list.txt lives in the snapshot dir and is removed with it.

            except Exception as e:
                print(f"❌ FAILED to prepend buffer. Using original event file. Error: {e}")
                input_for_repackaging = temp_event_ts

        # --- PRE-EVENT SNAPSHOT CLEANUP ---
        # The prepend above was the last step that needed the snapshot copies, so
        # bin them now. The live rolling buffer was never stopped, so there's
        # nothing to resume - it has been rolling (and staying full) the whole time.
        cleanup_event_snapshot(device_name, snapshot_dir)

        try:
            print(f"{device_name}: Repackaging to MP4 format...")
            ffmpeg_command = [
                'ffmpeg',
                '-i', input_for_repackaging,
                '-c:v', 'copy',
                '-af', 'aresample=48000,acompressor=threshold=-21dB:ratio=4:makeup=2,loudnorm=I=-16:LRA=7:tp=-1.5',
                '-c:a', 'aac',
                '-b:a', '48k',
                '-ac', '1',
                '-y',
                final_output_mp4
            ]
            subprocess.run(ffmpeg_command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            os.remove(input_for_repackaging)
            final_video_path = final_output_mp4

        except Exception as e:
            print(f"❌ FAILED to repackage to MP4. Post-processing might fail. Error: {e}")
            final_video_path = input_for_repackaging




        # --- POST-PROCESSING SECTION ---
        if not os.path.exists(final_video_path):
             print(f"❌ Final video file '{final_video_path}' not found. Cannot post-process.")
             return


        # Decide once whether this event actually generates an email. We already
        # returned early for NERFs, so reaching here means it's a confirmed event;
        # it still only emails if we're inside the alert window AND this camera/zone
        # is on the alarm-email list (see is_email_zone / conf.ini). Gating the
        # CPU-heavy attachment encode on the same decision means we don't burn
        # cycles (or leave orphaned temp files) encoding video we'll never send.
        video_to_attach = None
        if is_within_alert_window() and is_email_zone(device_name):
            print(f"{device_name}: In email window and zone allowed to email. Preparing email + video.")
            temp_folder = os.path.join(base_location, "temp")
            vid_quality = config.get('attachments', 'vid_attach_res', fallback='off')
            if vid_quality in ['720', '1080']:
                reencoded_path = os.path.join(temp_folder, f"{timestamp}-encoded.mp4")
                video_to_attach = reencode_video(final_video_path, reencoded_path, vid_quality)

                # --- SIZE CAP: 2-PASS VBR FALLBACK ---
                # If the re-encoded clip is still bigger than vid_max_size (MB),
                # sendmail will reject the message. Shrink it to a hard target
                # size with 2-pass VBR. We encode from the full-quality
                # final_video_path (not the already-encoded file) to avoid
                # stacking generation loss. vid_max_size <= 0 disables the cap.
                vid_max_size = config.getfloat('attachments', 'vid_max_size', fallback=0)
                if video_to_attach and vid_max_size > 0 and os.path.exists(video_to_attach):
                    actual_mb = os.path.getsize(video_to_attach) / (1024 * 1024)
                    if actual_mb > vid_max_size:
                        print(f"{device_name}: Attachment {actual_mb:.1f}MB > {vid_max_size}MB cap. Re-encoding 2-pass VBR.")
                        twopass_path = os.path.join(temp_folder, f"{timestamp}-encoded-2pass.mp4")
                        shrunk = reencode_video_2pass(final_video_path, twopass_path, vid_quality, vid_max_size)
                        if shrunk and os.path.exists(shrunk):
                            new_mb = os.path.getsize(shrunk) / (1024 * 1024)
                            print(f"{device_name}: 2-pass result {new_mb:.1f}MB.")
                            # Swap in the smaller file and drop the oversized one.
                            try:
                                os.remove(video_to_attach)
                            except Exception:
                                pass
                            video_to_attach = shrunk
                        else:
                            print(f"{device_name}: 2-pass shrink failed; sending original re-encode.")

            # Alert-window + zone gate already passed above, so just send. A None
            # video_to_attach (e.g. vid_attach_res = off, or an encode failure) is
            # fine - send_alert_email handles a snapshot-only email.
            email_thread = threading.Thread(
                target=send_alert_email,
                args=(device_name, TrigType, image_to_embed, video_to_attach)
            )
            email_thread.start()

        # Terminal log line for the recording thread, so the journal clearly shows
        # the event finished rather than appearing to hang on the last step (e.g.
        # "Repackaging...") whenever no email is sent. If an email WAS sent it runs
        # in its own thread and logs its own completion separately.
        print(f"{device_name}: Event processing complete.")


def extract_field(raw, key):
    """
    Pulls the value that follows  "key":  out of a raw camera payload.
    For example, given the key  Type  and a message containing
        ..."Type":"Manual"...
    this returns the string  Manual .

    There is deliberately NO JSON parsing here. We just hunt for the field
    we want and read the value sitting next to it. That means it doesn't
    matter if a cheap camera sends extra fields, a trailing null byte, odd
    spacing, or otherwise messy data - as long as the field is in there
    somewhere, we can pull it out.

    Returns the value as a string, or None if the key isn't found.
    """
    # 1. Look for the key exactly as it appears in the message, in quotes: "Type"
    marker = '"' + key + '"'
    key_pos = raw.find(marker)
    if key_pos == -1:
        return None  # this field isn't in the message at all

    # 2. Find the colon that separates the key from its value.
    colon_pos = raw.find(':', key_pos)
    if colon_pos == -1:
        return None

    # 3. The value starts just after the colon. Skip over any spaces.
    i = colon_pos + 1
    while i < len(raw) and raw[i] == ' ':
        i += 1

    # 4. The value is either wrapped in quotes (a string like "Manual")
    #    or bare (a number like 1). Handle each case separately.
    if i < len(raw) and raw[i] == '"':
        # Quoted value: step past the opening quote, then read until the
        # closing quote.
        i += 1
        start = i
        while i < len(raw) and raw[i] != '"':
            i += 1
        return raw[start:i]
    else:
        # Bare value: read until we hit a comma or the closing brace.
        start = i
        while i < len(raw) and raw[i] != ',' and raw[i] != '}':
            i += 1
        return raw[start:i].strip()


def process_message(message, addr):
    # Processes a single decoded alarm datagram and triggers or extends recordings.
    # With UDP there is no connection: one datagram = one complete message, so there
    # is no buffering/framing to do and no half-open connection to worry about.
    client_ip = addr[0]

    device_name = message.get('DeviceName', 'UnknownDevice')
    with seen_devices_lock:
        if device_name not in seen_device_names:
            print(f"🤝 New device seen: {device_name} from {client_ip}:{addr[1]}")
            seen_device_names.add(device_name)
            config = load_config()
            pre_event_seconds = config.getint('recording', 'pre_event_record_seconds', fallback=0)

            if pre_event_seconds > 0:
                # Start a rolling buffer for this device if one isn't already running.
                # NOTE: buffer_processes is keyed by device_name (see start_rolling_buffer),
                # so we must check against device_name here too. The original code checked
                # client_ip, which never matched and could spawn duplicate buffer threads.
                with buffer_lock:
                    if device_name not in buffer_processes:
                        buffer_thread = threading.Thread(
                            target=start_rolling_buffer,
                            args=(client_ip, config, device_name),
                            daemon=True
                        )
                        buffer_thread.start()

    # --- Main Trigger Logic ---
    TrigType = message.get('Type')
    Status = message.get('Status')
    device_name = message.get('DeviceName', 'UnknownDevice')
    if (Status == 1):  # denotes the start trigger.
        if (TrigType == 'Human Detect') or (TrigType == 'Manual'):
            ip_address = message.get('IP')
            config = load_config()
            duration = config.getint('recording', 'duration')
            with recording_lock:
                now = datetime.now()
                if device_name in currently_recording:
                    # Recording in progress: ANY re-trigger extends it (unconditional).
                    new_end_time = datetime.now() + timedelta(seconds=duration)
                    currently_recording[device_name]['end_time'] = new_end_time
                    currently_recording[device_name]['retrigger_count'] += 1
                    count = currently_recording[device_name]['retrigger_count']
                    max_time_sec = config.getint('recording', 'max_time_between_triggers', fallback=10)
                    last_trigger = currently_recording[device_name]['last_trigger_time']
                    time_since_last = (now - last_trigger).total_seconds()

                    # The recording is ALREADY extended above - every re-trigger,
                    # fast or slow, pushes out end_time. The check below only decides
                    # whether to *confirm* the event: a re-trigger that lands within
                    # max_time_sec of the previous one marks it as a real, sustained
                    # event (used later to decide on alerts / keeping the recording).
                    if time_since_last <= max_time_sec:
                        currently_recording[device_name]['rapid_succession_detected'] = True
                        print(f"{device_name}: Extending recording. Rapid trigger ({time_since_last:.1f}s, within {max_time_sec:.1f}s) - event confirmed.")
                    else:
                        print(f"{device_name}: Extending recording. (Slow re-trigger: {time_since_last:.1f}s)")
                    # ALWAYS update the last trigger time to this one
                    currently_recording[device_name]['last_trigger_time'] = now
                else:
                    # CASE 2: No recording, start a new one and set trigger count to 1
                    now = datetime.now()
                    initial_end_time = now + timedelta(seconds=duration)
                    currently_recording[device_name] = {
                        'end_time': initial_end_time,
                        'start_time': now,
                        'process': None,
                        'retrigger_count': 0
                    }
                    print(f"{device_name}: {TrigType} alarm Detected! Triggering new recording.")

                    # Start the recording manager thread
                    recorder_thread = threading.Thread(target=record_video, args=(device_name, ip_address, config, TrigType))
                    recorder_thread.start()
                currently_recording[device_name]['last_trigger_time'] = now
    else:
        #print(f"{device_name}: Recv status {Status}, ignoring.")  # 0 for event end.
        pass


def load_config():
    """
    Loads settings from conf.ini, located in the same directory as the script.
    Exits with an error if the file is not found or cannot be parsed.
    This is a bit more complicated than it probably needs to be as I was trying to figure out why it cound't find conf.
    Putting all the debug in fixed it, so here we are.
    """
    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        config_path = os.path.join(script_dir, 'conf.ini')

        # Check #1: Does the file exist at all?
        if not os.path.exists(config_path):
            # Using print with flush=True is best for systemd debugging
            print(f"FATAL: Configuration file not found at '{config_path}'", flush=True)
            sys.exit(1) # Exit with an error code

        config = configparser.ConfigParser()
        # config.read() returns a list of files it successfully parsed.
        # If the list is empty, the file was not read correctly (e.g., permissions issue).

        # Check #2: Was the file successfully read and parsed?
        if not config.read(config_path):
            print(f"FATAL: Could not read or parse config file at '{config_path}'", flush=True)
            sys.exit(1)

        return config

    except Exception as e:
        print(f"FATAL: An unexpected error occurred while loading config: {e}", flush=True)
        sys.exit(1)


def main():
    """
    Main function to start the UDP server.
    """
    cleanup_stale_buffers()
    config = load_config()
    host = config.get('server', 'host')
    port = config.getint('server', 'port')

    # Field names to look for in each camera's alarm message. These default to
    # the usual names but can be overridden in conf.ini, so a different camera
    # model that uses different keys is a config change, not a code change:
    #
    #   [message]
    #   field_type   = Type
    #   field_status = Status
    #   field_device = DeviceName
    #   field_ip     = IP
    f_type   = config.get('message', 'field_type',   fallback='Type')
    f_status = config.get('message', 'field_status', fallback='Status')
    f_device = config.get('message', 'field_device', fallback='DeviceName')
    f_ip     = config.get('message', 'field_ip',     fallback='IP')

    # UDP is connectionless: a silently-dropped link can't leave us stuck in a
    # half-open state the way TCP could, and message boundaries are preserved
    # (one datagram == one message). SO_REUSEADDR lets us rebind immediately on
    # restart, so the old netstat/lsof "free_port" hack is no longer needed.
    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((host, port))
    print(f"🚀 UDP server listening on {host}:{port}")

    housekeeper = threading.Thread(target=housekeeping_daemon, daemon=True)
    housekeeper.start()

    while True:
        try:
            # 65535 comfortably exceeds any single alarm datagram; recvfrom returns
            # exactly one complete message at a time.
            data, addr = server.recvfrom(65535)
        except OSError as e:
            # On some platforms a previous send can surface a stale ICMP error here.
            print(f"⚠️ recvfrom error: {e}", flush=True)
            continue

        # Turn the raw bytes into text. errors='replace' means a stray bad byte
        # becomes a placeholder character instead of crashing the decode.
        raw = data.decode('utf-8', errors='replace')

        # Pull out only the fields we care about. No JSON parsing, so messy or
        # null-terminated payloads from cheap cameras don't trip us up.
        message = {
            'Type':       extract_field(raw, f_type),
            'Status':     extract_field(raw, f_status),
            'DeviceName': extract_field(raw, f_device),
            'IP':         extract_field(raw, f_ip),
        }

        # Status is compared as a number further down, so convert it now.
        try:
            message['Status'] = int(message['Status'])
        except (TypeError, ValueError):
            message['Status'] = None

        # If the essentials are missing, the datagram is unusable - log the raw
        # bytes (so you can always eyeball a bad message) and move on.
        if not message['DeviceName'] or message['Status'] is None:
            print(f"⚠️ Unusable datagram from {addr[0]}:{addr[1]}: {data!r}", flush=True)
            continue

        # process_message is quick and offloads recording to its own threads, so we
        # handle it inline and keep the receive loop responsive.
        try:
            process_message(message, addr)
        except Exception as e:
            print(f"⚠️ Error handling message from {addr[0]}: {e}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        # This allows you to stop the script cleanly with Ctrl+C
        print("\n👋 Shutdown requested by user. Exiting.")
        sys.exit(0)
    except Exception as e:
        # This is the catch-all for any other unexpected error
        print(f"\n💥 FATAL ERROR: A critical exception occurred. The script will terminate.")
        # The traceback prints the full error details, which is vital for debugging.
        traceback.print_exc()
        sys.exit(1) # Exit with a non-zero code to indicate an error
