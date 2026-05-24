from flask import Flask, request, jsonify, send_file, send_from_directory
from flask_cors import CORS
import yt_dlp
import re
import uuid
import os
import threading
import time
import subprocess
import smtplib
import requests
import platform
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from datetime import datetime, timedelta
import warnings
import logging
from urllib.parse import unquote
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Suppress all warnings
warnings.filterwarnings("ignore")
logging.getLogger('yt_dlp').setLevel(logging.ERROR)
os.environ['YTDLP_NO_UPDATE'] = '1'

app = Flask(__name__, static_folder='../', static_url_path='')
CORS(app)

# ============================================================
# CROSS-PLATFORM DOWNLOAD FOLDER DETECTION
# ============================================================
def get_download_folder():
    env = os.environ.get('ENVIRONMENT', 'CLOUD')
    
    # LOCAL DEVELOPMENT (Termux/Your PC)
    if env == 'LOCAL':
        system = platform.system()
        
        # Android (Termux)
        if system == "Linux" and Path("/storage/emulated/0").exists():
            android_paths = [
                "/storage/emulated/0/Download/Saverlian",
                "/sdcard/Download/Saverlian",
                "/storage/self/primary/Download/Saverlian"
            ]
            for path_str in android_paths:
                try:
                    path = Path(path_str)
                    path.mkdir(parents=True, exist_ok=True)
                    return path
                except:
                    continue
            return Path.home() / "storage" / "downloads" / "Saverlian"
        
        # Windows, Mac, Linux Desktop
        else:
            return Path.home() / "Downloads" / "Saverlian"
    
    # CLOUD HOSTING (Railway, Koyeb, Render)
    else:
        return Path("/tmp/saverlian_downloads")

DOWNLOAD_FOLDER = get_download_folder()
try:
    DOWNLOAD_FOLDER.mkdir(parents=True, exist_ok=True)
    print(f"📁 Download folder: {DOWNLOAD_FOLDER}")
except Exception as e:
    print(f"⚠️ Could not create folder: {e}")
    DOWNLOAD_FOLDER = Path.cwd() / "downloads"
    DOWNLOAD_FOLDER.mkdir(parents=True, exist_ok=True)
    print(f"📁 Using fallback: {DOWNLOAD_FOLDER}")

# ============================================================
# FFMPEG HEALTH CHECK
# ============================================================
def check_ffmpeg():
    try:
        result = subprocess.run(['ffmpeg', '-version'], capture_output=True, text=True, timeout=5)
        return result.returncode == 0
    except:
        return False

FFMPEG_AVAILABLE = check_ffmpeg()
if not FFMPEG_AVAILABLE:
    print("⚠️ FFmpeg not found. Trim and compression features disabled.")

# ============================================================
# CONFIGURATION
# ============================================================
MAX_CONCURRENT_DOWNLOADS = 2
active_downloads_semaphore = threading.Semaphore(MAX_CONCURRENT_DOWNLOADS)

download_status = {}
downloading = {}
paused_downloads = {}
scheduled_downloads = {}
download_timestamps = {}

# Email configuration from environment variables
EMAIL_SENDER = os.environ.get('EMAIL_SENDER', '')
EMAIL_PASSWORD = os.environ.get('EMAIL_PASSWORD', '')
if not EMAIL_SENDER or not EMAIL_PASSWORD:
    print("⚠️ Email credentials not set. Email features disabled.")

# ============================================================
# HELPER FUNCTIONS
# ============================================================
def cleanup_old_status():
    expire_time = datetime.now() - timedelta(hours=24)
    to_remove = [did for did, ts in download_timestamps.items() if ts < expire_time]
    for did in to_remove:
        download_status.pop(did, None)
        downloading.pop(did, None)
        paused_downloads.pop(did, None)
        scheduled_downloads.pop(did, None)
        download_timestamps.pop(did, None)

def validate_time_format(time_str):
    if not time_str:
        return True
    pattern = r'^(\d{1,2}:)?\d{1,2}:\d{2}$'
    if not re.match(pattern, time_str):
        return False
    parts = time_str.split(':')
    if len(parts) == 2:
        minutes, seconds = int(parts[0]), int(parts[1])
        return 0 <= minutes < 60 and 0 <= seconds < 60
    elif len(parts) == 3:
        hours, minutes, seconds = int(parts[0]), int(parts[1]), int(parts[2])
        return 0 <= hours < 24 and 0 <= minutes < 60 and 0 <= seconds < 60
    return False

def clean_filename(name):
    if not name:
        return "video"
    name = re.sub(r'[^a-zA-Z0-9_\-\.]', '', name)
    if len(name) > 50:
        name = name[:50]
    name = name.rstrip('_.-')
    return name if name else "video"

def is_safe_path(file_path, base_folder):
    try:
        resolved_path = file_path.resolve()
        resolved_base = base_folder.resolve()
        return resolved_base in resolved_path.parents or resolved_path == resolved_base
    except:
        return False

def format_size(bytes):
    if not bytes or bytes == 0:
        return "0 B"
    for unit in ['B', 'KB', 'MB', 'GB']:
        if bytes < 1024:
            return f"{bytes:.1f}{unit}"
        bytes /= 1024
    return f"{bytes:.1f}GB"

def format_speed(bytes_per_sec):
    if not bytes_per_sec or bytes_per_sec == 0:
        return "0 B/s"
    return format_size(bytes_per_sec) + "/s"

def get_unique_filename(base_path):
    if not base_path.exists():
        return base_path
    stem = base_path.stem
    suffix = base_path.suffix
    if len(stem) > 45:
        stem = stem[:45]
    counter = 1
    while True:
        new_path = base_path.parent / f"{stem}_{counter}{suffix}"
        if not new_path.exists():
            return new_path
        counter += 1

def time_to_seconds(time_str):
    if not time_str:
        return None
    parts = time_str.split(':')
    if len(parts) == 2:
        return int(parts[0]) * 60 + int(parts[1])
    elif len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    return None

def trim_video(input_path, output_path, start_time, end_time):
    if not FFMPEG_AVAILABLE:
        return False
    if not validate_time_format(start_time) or not validate_time_format(end_time):
        return False
    try:
        cmd = ['ffmpeg', '-i', str(input_path), '-ss', start_time, '-to', end_time, 
               '-c', 'copy', '-avoid_negative_ts', 'make_zero', '-y', str(output_path)]
        subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        return True
    except:
        return False

def compress_video(input_path, output_path):
    if not FFMPEG_AVAILABLE:
        return False
    try:
        cmd = ['ffmpeg', '-i', str(input_path), '-vf', 'scale=1280:-2', 
               '-b:v', '1M', '-b:a', '128k', '-y', str(output_path)]
        subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        return True
    except:
        return False

def send_email_notification(filename, file_path, recipient_email):
    if not EMAIL_SENDER or not EMAIL_PASSWORD:
        return False
    try:
        print(f"📧 Sending email to: {recipient_email}")
        msg = MIMEMultipart()
        msg['From'] = f"Saverlian <{EMAIL_SENDER}>"
        msg['To'] = recipient_email
        msg['Subject'] = f'Saverlian - Download Complete: {filename}'
        
        body = f"""
        <h2>Your download is ready!</h2>
        <p>File: <strong>{filename}</strong></p>
        <p>Size: {format_size(file_path.stat().st_size)}</p>
        <p>Saved to: Saverlian folder</p>
        <br>
        <p>Thank you for using Saverlian!</p>
        """
        msg.attach(MIMEText(body, 'html'))
        
        with smtplib.SMTP('smtp.gmail.com', 587) as server:
            server.starttls()
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.send_message(msg)
        
        print(f"✅ Email sent to {recipient_email}")
        return True
    except Exception as e:
        print(f"❌ Email failed: {e}")
        return False

def send_telegram_notification(bot_token, chat_id, filename, file_path):
    if not bot_token or not re.match(r'^\d+:[A-Za-z0-9_-]+$', bot_token):
        return False
    try:
        url = f"https://api.telegram.org/bot{bot_token}/sendDocument"
        with open(file_path, 'rb') as file:
            files = {'document': (filename, file)}
            data = {'chat_id': chat_id, 'caption': f'Download ready: {filename}'}
            response = requests.post(url, files=files, data=data, timeout=30)
        return response.status_code == 200
    except Exception as e:
        print(f"Telegram error: {e}")
        return False

def get_error_category(error_msg):
    error_lower = error_msg.lower()
    if "network" in error_lower or "connection" in error_lower or "timeout" in error_lower:
        return "Network error. Please check your internet connection."
    elif "ffmpeg" in error_lower:
        return "FFmpeg not installed. Trim/compression features unavailable."
    elif "unavailable" in error_lower or "private" in error_lower:
        return "Video unavailable or private. Cannot download."
    elif "rate" in error_lower or "quota" in error_lower:
        return "Rate limited. Please try again in a few minutes."
    elif "format" in error_lower:
        return "Video format not available. Try a different quality."
    elif "permission" in error_lower or "access" in error_lower:
        return "Permission denied. Cannot save file."
    else:
        return error_msg[:100]

def download_worker(download_id, url, quality, download_title, download_type, audio_format, advanced_options=None, retry_count=0):
    max_retries = 3
    try:
        title = clean_filename(download_title)
        
        if download_type == 'audio':
            audio_ext_map = {
                'mp3_320': 'mp3', 'mp3_256': 'mp3', 'mp3_192': 'mp3', 'mp3_128': 'mp3',
                'm4a': 'm4a', 'ogg': 'ogg', 'wav': 'wav'
            }
            ext = audio_ext_map.get(audio_format, 'mp3')
            safe_title = title[:50] if len(title) > 50 else title
            base_output = DOWNLOAD_FOLDER / f"{safe_title}.{ext}"
            temp_path = DOWNLOAD_FOLDER / f"{safe_title}_temp.{ext}"
            bitrate = audio_format.split('_')[1] if audio_format and audio_format.startswith('mp3') else '192'
        else:
            safe_title = title[:50] if len(title) > 50 else title
            base_output = DOWNLOAD_FOLDER / f"{safe_title}.mp4"
            temp_path = DOWNLOAD_FOLDER / f"{safe_title}_temp.mp4"
            bitrate = None
        
        output_path = get_unique_filename(base_output)
        
        if download_type == 'audio':
            if audio_format == 'm4a':
                format_spec = 'bestaudio[ext=m4a]/bestaudio'
            elif audio_format == 'ogg':
                format_spec = 'bestaudio[ext=ogg]/bestaudio'
            elif audio_format == 'wav':
                format_spec = 'bestaudio[ext=wav]/bestaudio'
            else:
                format_spec = 'bestaudio/best'
            
            ydl_opts = {
                'format': format_spec,
                'outtmpl': str(temp_path),
                'quiet': True,
                'no_warnings': True,
                'ignoreerrors': True,
                'noplaylist': True,
                'continuedl': True,
                'retries': 10,
                'fragment_retries': 10,
                'skip_unavailable_fragments': True,
                'progress_hooks': [lambda d: progress_hook(d, download_id)],
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': bitrate,
                }] if audio_format and audio_format.startswith('mp3') else []
            }
        else:
            ydl_opts = {
                'format': 'best[ext=mp4]/best',
                'outtmpl': str(temp_path),
                'quiet': True,
                'no_warnings': True,
                'ignoreerrors': True,
                'noplaylist': True,
                'continuedl': True,
                'retries': 10,
                'fragment_retries': 10,
                'skip_unavailable_fragments': True,
                'progress_hooks': [lambda d: progress_hook(d, download_id)],
            }
        
        if advanced_options and advanced_options.get('trim_start') and advanced_options.get('trim_end'):
            if validate_time_format(advanced_options['trim_start']) and validate_time_format(advanced_options['trim_end']):
                start_sec = time_to_seconds(advanced_options['trim_start'])
                end_sec = time_to_seconds(advanced_options['trim_end'])
                if start_sec is not None and end_sec is not None and start_sec < end_sec:
                    ydl_opts['download_ranges'] = lambda info, _: [{
                        'start_time': start_sec,
                        'end_time': end_sec
                    }]
        
        download_status[download_id] = {'status': 'downloading', 'progress': 0, 'title': title}
        download_timestamps[download_id] = datetime.now()
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            downloading[download_id] = True
            
            try:
                ydl.download([url])
                
                if downloading.get(download_id, False):
                    if not temp_path.exists():
                        raise Exception("No file created")
                    
                    final_path = temp_path
                    
                    if advanced_options and advanced_options.get('trim_start') and advanced_options.get('trim_end'):
                        if validate_time_format(advanced_options['trim_start']) and validate_time_format(advanced_options['trim_end']):
                            download_status[download_id] = {'status': 'trimming', 'progress': 90}
                            trimmed_path = DOWNLOAD_FOLDER / f"temp_trimmed_{download_id}.mp4"
                            if trim_video(temp_path, trimmed_path, advanced_options['trim_start'], advanced_options['trim_end']):
                                final_path = trimmed_path
                                if temp_path.exists():
                                    temp_path.unlink()
                    
                    if advanced_options and advanced_options.get('compress'):
                        download_status[download_id] = {'status': 'compressing', 'progress': 95}
                        compressed_path = DOWNLOAD_FOLDER / f"temp_compressed_{download_id}.mp4"
                        if compress_video(final_path, compressed_path):
                            if final_path != temp_path and final_path.exists():
                                final_path.unlink()
                            final_path = compressed_path
                    
                    final_path.rename(output_path)
                    
                    file_size = output_path.stat().st_size
                    if file_size < 1000:
                        raise Exception("File size too small")
                    
                    if advanced_options and advanced_options.get('email'):
                        download_status[download_id] = {'status': 'sending_email', 'progress': 98}
                        send_email_notification(output_path.name, output_path, advanced_options['email'])
                    
                    if advanced_options and advanced_options.get('telegram_bot') and advanced_options.get('telegram_chat'):
                        download_status[download_id] = {'status': 'sending_telegram', 'progress': 99}
                        send_telegram_notification(
                            advanced_options['telegram_bot'], 
                            advanced_options['telegram_chat'], 
                            output_path.name, 
                            output_path
                        )
                    
                    download_status[download_id] = {
                        'status': 'completed',
                        'progress': 100,
                        'filename': output_path.name,
                        'file_size': file_size,
                        'file_size_str': format_size(file_size)
                    }
                    
            except Exception as e:
                error_msg = str(e)
                if downloading.get(download_id, False):
                    if retry_count < max_retries:
                        print(f"⚠️ Retry {retry_count + 1}/{max_retries} for {download_id}")
                        time.sleep(3)
                        download_worker(download_id, url, quality, download_title, download_type, 
                                      audio_format, advanced_options, retry_count + 1)
                    else:
                        download_status[download_id] = {'status': 'error', 'error': get_error_category(error_msg)}
            finally:
                if download_id in downloading:
                    del downloading[download_id]
                if download_id in paused_downloads:
                    del paused_downloads[download_id]
                cleanup_old_status()
                
    except Exception as e:
        if download_id in downloading:
            del downloading[download_id]
        download_status[download_id] = {'status': 'error', 'error': get_error_category(str(e))}

def progress_hook(d, download_id):
    if download_id in paused_downloads and paused_downloads[download_id]:
        raise Exception("Paused")
    
    if d['status'] == 'downloading' and 'total_bytes' in d and d['total_bytes'] > 0:
        percent = (d['downloaded_bytes'] / d['total_bytes']) * 100
        download_status[download_id] = {
            'status': 'downloading',
            'progress': round(percent, 1),
            'downloaded': d['downloaded_bytes'],
            'total': d['total_bytes'],
            'speed': d.get('speed', 0),
            'downloaded_str': format_size(d['downloaded_bytes']),
            'total_str': format_size(d['total_bytes']),
            'speed_str': format_speed(d.get('speed', 0))
        }

def schedule_worker(download_id, url, quality, title, download_type, audio_format, advanced_options, scheduled_time):
    while datetime.now() < scheduled_time:
        time.sleep(30)
        if download_id not in scheduled_downloads:
            return
    if download_id in scheduled_downloads:
        del scheduled_downloads[download_id]
        download_worker(download_id, url, quality, title, download_type, audio_format, advanced_options)

# ============================================================
# ROUTES
# ============================================================
@app.route('/')
def serve_index():
    return send_from_directory('..', 'index.html')

@app.route('/beauty/<path:path>')
def serve_beauty(path):
    return send_from_directory('../beauty', path)

@app.route('/functions/<path:path>')
def serve_functions(path):
    return send_from_directory('../functions', path)

@app.route('/images/<path:path>')
def serve_images(path):
    return send_from_directory('../images', path)

@app.route('/api/health', methods=['GET'])
def health_check():
    return jsonify({
        'status': 'ok',
        'ffmpeg': FFMPEG_AVAILABLE,
        'download_folder': str(DOWNLOAD_FOLDER)
    })

@app.route('/api/get_info', methods=['POST'])
def get_video_info():
    try:
        data = request.get_json()
        url = data.get('url', '').strip()
        
        if not url:
            return jsonify({'error': 'URL is required'}), 400
        
        ydl_opts = {'quiet': True, 'no_warnings': True}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            duration = info.get('duration', 0)
            duration_str = f"{duration // 60}:{duration % 60:02d}" if duration else "Unknown"
            
            formats = []
            for f in info.get('formats', []):
                if f.get('vcodec') != 'none':
                    formats.append({
                        'format_id': f.get('format_id'),
                        'resolution': f.get('height', 'audio'),
                        'ext': f.get('ext', 'mp4'),
                        'filesize': f.get('filesize', 0)
                    })
            
            return jsonify({
                'success': True,
                'title': info.get('title', 'Unknown'),
                'thumbnail': info.get('thumbnail', ''),
                'uploader': info.get('uploader', 'Unknown'),
                'duration': duration_str,
                'duration_seconds': duration,
                'formats': formats[:10]
            })
    except Exception as e:
        return jsonify({'error': get_error_category(str(e))}), 500

@app.route('/api/download', methods=['POST'])
def start_download():
    try:
        data = request.get_json()
        url = data.get('url', '').strip()
        quality = data.get('format_id', 'best')
        download_type = data.get('download_type', 'video')
        audio_format = data.get('audio_format', 'mp3_192')
        trim_start = data.get('trim_start')
        trim_end = data.get('trim_end')
        compress = data.get('compress', False)
        schedule_time = data.get('schedule_time')
        email = data.get('email')
        telegram_bot = data.get('telegram_bot')
        telegram_chat = data.get('telegram_chat')
        
        if not url:
            return jsonify({'error': 'URL required'}), 400
        
        with yt_dlp.YoutubeDL({'quiet': True}) as ydl:
            info = ydl.extract_info(url, download=False)
            title = info.get('title', 'video')
        
        download_id = str(uuid.uuid4())[:8]
        
        advanced_options = {
            'trim_start': trim_start, 'trim_end': trim_end, 'compress': compress,
            'email': email, 'telegram_bot': telegram_bot, 'telegram_chat': telegram_chat
        }
        
        if (trim_start or trim_end or compress) and not FFMPEG_AVAILABLE:
            return jsonify({'error': 'FFmpeg not available. Trim and compression features disabled.'}), 400
        
        if schedule_time:
            try:
                scheduled_dt = datetime.fromisoformat(schedule_time)
                if scheduled_dt > datetime.now():
                    scheduled_downloads[download_id] = True
                    download_status[download_id] = {'status': 'scheduled', 'progress': 0, 'scheduled_time': schedule_time, 'title': clean_filename(title)}
                    thread = threading.Thread(target=schedule_worker, args=(download_id, url, quality, title, download_type, audio_format, advanced_options, scheduled_dt))
                    thread.daemon = True
                    thread.start()
                    return jsonify({'success': True, 'download_id': download_id, 'scheduled': True})
            except:
                pass
        
        with active_downloads_semaphore:
            thread = threading.Thread(target=download_worker, args=(download_id, url, quality, title, download_type, audio_format, advanced_options))
            thread.daemon = True
            thread.start()
        
        return jsonify({'success': True, 'download_id': download_id})
        
    except Exception as e:
        return jsonify({'error': get_error_category(str(e))}), 500

@app.route('/api/pause/<download_id>', methods=['POST'])
def pause_download(download_id):
    if download_id in downloading:
        paused_downloads[download_id] = True
        download_status[download_id]['status'] = 'paused'
        return jsonify({'success': True})
    return jsonify({'error': 'Download not found'}), 404

@app.route('/api/resume/<download_id>', methods=['POST'])
def resume_download(download_id):
    try:
        data = request.get_json()
        url = data.get('url')
        quality = data.get('format_id', 'best')
        download_type = data.get('download_type', 'video')
        audio_format = data.get('audio_format', 'mp3_192')
        
        if not url:
            return jsonify({'error': 'URL required'}), 400
        
        title = download_status.get(download_id, {}).get('title', '')
        if not title:
            with yt_dlp.YoutubeDL({'quiet': True}) as ydl:
                info = ydl.extract_info(url, download=False)
                title = info.get('title', 'video')
        
        new_download_id = str(uuid.uuid4())[:8]
        advanced_options = {
            'trim_start': data.get('trim_start'), 'trim_end': data.get('trim_end'),
            'compress': data.get('compress', False), 'email': data.get('email'),
            'telegram_bot': data.get('telegram_bot'), 'telegram_chat': data.get('telegram_chat')
        }
        
        with active_downloads_semaphore:
            thread = threading.Thread(target=download_worker, args=(new_download_id, url, quality, title, download_type, audio_format, advanced_options))
            thread.daemon = True
            thread.start()
        
        download_status[new_download_id] = download_status.get(download_id, {})
        return jsonify({'success': True, 'download_id': new_download_id})
    except Exception as e:
        return jsonify({'error': get_error_category(str(e))}), 500

@app.route('/api/cancel/<download_id>', methods=['POST'])
def cancel_download(download_id):
    if download_id in downloading:
        downloading[download_id] = False
    if download_id in download_status:
        download_status[download_id] = {'status': 'cancelled'}
    if download_id in scheduled_downloads:
        del scheduled_downloads[download_id]
    for temp_file in DOWNLOAD_FOLDER.glob("*_temp*"):
        if download_id in str(temp_file):
            try:
                temp_file.unlink()
            except:
                pass
    return jsonify({'success': True})

@app.route('/api/status/<download_id>')
def get_status(download_id):
    if download_id in download_status:
        return jsonify(download_status[download_id])
    return jsonify({'status': 'pending'}), 200

@app.route('/api/get_file/<filename>')
def get_file(filename):
    filename = unquote(filename)
    safe_filename = clean_filename(filename)
    file_path = DOWNLOAD_FOLDER / safe_filename
    
    if not is_safe_path(file_path, DOWNLOAD_FOLDER):
        return jsonify({'error': 'Invalid file path'}), 403
    
    if file_path.exists():
        return send_file(file_path, as_attachment=True, download_name=safe_filename)
    
    for file in DOWNLOAD_FOLDER.iterdir():
        if is_safe_path(file, DOWNLOAD_FOLDER) and (safe_filename in file.name or file.name.startswith(safe_filename[:40])):
            return send_file(file, as_attachment=True, download_name=file.name)
    
    return jsonify({'error': 'File not found'}), 404

@app.route('/api/cleanup_temp', methods=['POST'])
def cleanup_temp_files():
    try:
        cleaned = 0
        for file in DOWNLOAD_FOLDER.glob("*_temp*"):
            if is_safe_path(file, DOWNLOAD_FOLDER):
                if time.time() - file.stat().st_mtime > 3600:
                    file.unlink()
                    cleaned += 1
        cleanup_old_status()
        return jsonify({'success': True, 'cleaned': cleaned})
    except Exception as e:
        return jsonify({'error': str(e)[:100]}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f"\n{'='*40}")
    print(f" Saverlian Downloader")
    print(f" Save: {DOWNLOAD_FOLDER}")
    print(f" FFmpeg: {'Available' if FFMPEG_AVAILABLE else 'Not available'}")
    print(f" Email: {'Configured' if EMAIL_SENDER else 'Not configured'}")
    print(f"{'='*40}\n")
    app.run(debug=False, host='0.0.0.0', port=port)