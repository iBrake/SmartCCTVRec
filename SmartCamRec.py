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
            if os.path.isdir(item_path) and item.startswith("cctv_buffer_"):
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
        
def record_video(device_name, ip_address, config, TrigType):
    #Manages a recording session and triggers post-recording actions like email alerts.
    
    global currently_recording, buffer_processes, buffer_lock
    
    # --- 1. PAUSE THE ROLLING BUFFER & CHECK IF READY ---
    buffer_proc = None
    is_buffer_ready = False  # Flag to track if we have a full pre-event buffer
    
    try:
        # Get buffer config
        pre_event_seconds = config.getint('recording', 'pre_event_record_seconds')
        
        with buffer_lock:
            # Find the buffer process for this camera's IP
            buffer_proc = buffer_processes.get(device_name)
            if buffer_proc and buffer_proc.poll() is None:
                print(f"{device_name}: Pausing pre-event buffer.")
                buffer_proc.send_signal(signal.SIGSTOP)
                
                buffer_dir = f"/dev/shm/cctv_buffer_{device_name}"
                if pre_event_seconds > 0:
                    if os.path.isdir(buffer_dir):
                        try:
                            # List all the .ts segment files in the buffer directory
                            ts_files = [f for f in os.listdir(buffer_dir) if f.endswith('.ts')]
                            
                            # Check if we have *at least* the required number of segments
                            if len(ts_files) >= pre_event_seconds:
                                print(f"{device_name}: Buffer is full ({len(ts_files)} segments). Pre-event capture is ready.")
                                is_buffer_ready = True
                            else:
                                print(f"{device_name}: Pre-event is not full ({len(ts_files)}/{pre_event_seconds}). Not using.")
                        except Exception as e:
                            print(f"❌ Error checking buffer directory {buffer_dir}: {e}")
                    else:
                        print(f"{device_name}: Pre-event buffer not ready yet. Not using.")
                        
    except Exception as e:
        print(f"⚠️ Warning: Could not pause rolling buffer for {device_name}. {e}")
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

        if is_buffer_ready and os.path.exists(temp_event_ts):
            print(f"{device_name}: Adding pre-event buffer..")
            
            buffer_dir = f"/dev/shm/cctv_buffer_{device_name}"
            concat_list_path = os.path.join(buffer_dir, 'concat_list.txt')
            combined_ts_file = f"{os.path.splitext(temp_event_ts)[0]}-COMBINED.ts"

            try:
                # --- A. Get the original buffer segments ---
                all_ts_files = []
                for f in os.listdir(buffer_dir):
                    if f.endswith('.ts'):
                        file_path = os.path.join(buffer_dir, f)
                        all_ts_files.append((file_path, os.path.getmtime(file_path)))
                all_ts_files.sort(key=lambda x: x[1])
                pre_event_seconds = config.getint('recording', 'pre_event_record_seconds')
                segment_files = [f[0] for f in all_ts_files[-pre_event_seconds:]]
                
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
                os.remove(concat_list_path)
            
            except Exception as e:
                print(f"❌ FAILED to prepend buffer. Using original event file. Error: {e}")
                input_for_repackaging = temp_event_ts
        
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


        
        # --- BUFFER & RECORDING SLOT CLEANUP ---
        try:
            clear_ram_buffer(device_name)
        except Exception as e:
            print(f"⚠️ Warning: Could not clear rolling buffer. {e}")
       
        try:
            with buffer_lock:
                if buffer_proc and buffer_proc.poll() is None:
                    print(f"{device_name}: Resuming rolling buffer.")
                    buffer_proc.send_signal(signal.SIGCONT)
        except Exception as e:
            print(f"⚠️ Warning: Could not resume rolling buffer for {device_name}")
 
        # --- POST-PROCESSING SECTION ---
        if not os.path.exists(final_video_path):
             print(f"❌ Final video file '{final_video_path}' not found. Cannot post-process.")
             return
        

        #Reencode video if it's valid. NERFs should be deleted unless debug.
        if rapid_succession_detected:
            print(f"{device_name}: In email time, retrigger count met. Getting email and video.")
            temp_folder = os.path.join(base_location, "temp")
            video_to_attach = None
            vid_quality = config.get('attachments', 'vid_attach_res', fallback='off')
            if vid_quality in ['720', '1080']:
                reencoded_path = os.path.join(temp_folder, f"{timestamp}-encoded.mp4")
                video_to_attach = reencode_video(final_video_path, reencoded_path, vid_quality)
        
        #if we are in alert time, send an email. rapid_succession_detected checked again, in case nerfs.
        if is_within_alert_window() and rapid_succession_detected:
            email_thread = threading.Thread(
                target=send_alert_email,
                args=(device_name, TrigType, image_to_embed, video_to_attach)
            )
            email_thread.start()
        
            
            
def handle_client(client_socket, client_addr):
    #Handles an incoming connection and triggers or extends recordings.
    
    client_ip = client_addr[0]
    
    try:
        buffer = ""
        while True:
            data = client_socket.recv(1024).decode('utf-8')
            if not data:
                break # Connection closed

            buffer += data
            # The camera might send multiple JSON objects together or one object in multiple packets.
            # We look for the closing brace '}' to identify a complete message.
            while '}' in buffer:
                end_index = buffer.find('}') + 1
                json_str = buffer[:end_index]
                buffer = buffer[end_index:] # Keep the rest of the buffer

                try:
                    message = json.loads(json_str)
                    
                    device_name = message.get('DeviceName', 'UnknownDevice')
                    with seen_devices_lock:
                        if device_name not in seen_device_names:
                            print(f"🤝 New device connected from {client_ip}:{client_addr[1]}")
                            seen_device_names.add(device_name)
                            #connected_clients.add(client_ip)
                            config = load_config()
                            pre_event_seconds = config.getint('recording', 'pre_event_record_seconds', fallback=0)
                            
                            if pre_event_seconds > 0:
                                # Check if a buffer isn't already running for this IP
                                with buffer_lock:
                                    if client_ip not in buffer_processes:
                                        # Start the buffer in a new daemon thread
                                        buffer_thread = threading.Thread(
                                            target=start_rolling_buffer, 
                                            args=(client_ip,config,device_name), 
                                            daemon=True
                                        )
                                        buffer_thread.start()
                    
                    #print(f"Received data: {message}")

                    # --- Main Trigger Logic ---
                    TrigType = message.get('Type')
                    Status = message.get('Status')
                    device_name = message.get('DeviceName', 'UnknownDevice')
                    if (Status  == 1): #denotes the start trigger.
                        if (TrigType == 'Human Detect') or (TrigType == 'Manual'):
                            ip_address = message.get('IP')
                            config = load_config()
                            duration = config.getint('recording', 'duration')
                            #extendtime = config.getint('recording', 'extendtime')
                            with recording_lock:
                                now = datetime.now()
                                if device_name in currently_recording:
                                    #Recording in progress, extend it and increment trigger count
                                    new_end_time = datetime.now() + timedelta(seconds=duration)
                                    currently_recording[device_name]['end_time'] = new_end_time
                                    currently_recording[device_name]['retrigger_count'] += 1
                                    count = currently_recording[device_name]['retrigger_count']
                                    #print(f"{device_name}: Retriggered. Extending recording. Retrigger count:{count}")
                                    max_time_sec = config.getint('recording', 'max_time_between_triggers', fallback=10)
                                    last_trigger = currently_recording[device_name]['last_trigger_time']
                                    time_since_last = (now - last_trigger).total_seconds()
                                    
                                    rapid_succession_detected = False
                                    # Check if this trigger is "rapid"
                                    if time_since_last <= max_time_sec and not rapid_succession_detected:
                                        # We have a confirmed event!
                                        currently_recording[device_name]['rapid_succession_detected'] = True
                                        rapid_succession_detected = True
                                        print(f"{device_name}: Rapid trigger. ({time_since_last:.1f}s), within {max_time_sec:.1f}s. Event confirmed.")
                                    else:
                                        print(f"{device_name}: Extending recording. (Slow re-trigger: {time_since_last:.1f}s)")
                                    # ALWAYS update the last trigger time to this one
                                    currently_recording[device_name]['last_trigger_time'] = now
                                    # -----------------------------
                                else:
                                    # --- THIS BLOCK IS UPDATED ---
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
                        print (f"{device_name}: Recv status {Status}, ignoring.") #0 for event end.
                except json.JSONDecodeError:
                    print(f"⚠️ Received incomplete or malformed JSON: '{json_str}'")

    except ConnectionResetError:
        print("Client disconnected.")
    finally:
        client_socket.close()

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
    Main function to start the server.
    """
    cleanup_stale_buffers()
    config = load_config()
    host = config.get('server', 'host')
    port = config.getint('server', 'port')

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    free_port(port)
    server.bind((host, port))
    server.listen(5) # Listen for up to 5 simultaneous connections
    print(f"🚀 Server listening on {host}:{port}")
    
    housekeeper = threading.Thread(target=housekeeping_daemon, daemon=True)
    housekeeper.start()
    
    while True:
        client, addr = server.accept()
        client_handler = threading.Thread(target=handle_client, args=(client, addr))
        client_handler.start()

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