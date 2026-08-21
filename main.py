import os
import re
import shutil
import threading
from flask import Flask, render_template, send_file, request, jsonify
from flask_cors import CORS
from flask_socketio import SocketIO, emit
import yt_dlp

app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret!'
CORS(app)

@app.route('/')
def index():
    return render_template('index.html')

socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# 1. Dynamic FFmpeg Detection
try:
    import imageio_ffmpeg
    ffmpeg_dir = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    ffmpeg_dir = shutil.which('ffmpeg')
    if not ffmpeg_dir:
        local_appdata = os.environ.get('LOCALAPPDATA', '')
        ffmpeg_dir = os.path.join(
            local_appdata,
            'Microsoft', 'WinGet', 'Packages',
            'Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe',
            'ffmpeg-9.0-full_build', 'bin'
        )

DOWNLOAD_FOLDER = os.path.join(os.getcwd(), 'downloads')
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

def sanitize_filename(filename):
    return re.sub(r'[\\/*?:"<>|]', "", filename)

@app.route('/fetch-info', methods=['POST'])
def fetch_info():
    data = request.json or {}
    url = data.get('url')
    if not url:
        return jsonify({'error': 'URL required'}), 400

    try:
        ydl_opts = {
            'quiet': True,
            'skip_download': True,
            'extractor_args': {'youtube': {'player_client': ['web_safari', 'web']}}
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

            # Format duration in MM:SS
            duration_secs = info.get('duration', 0)
            mins, secs = divmod(duration_secs, 60)
            duration_str = f"{mins}:{secs:02d}"

            return jsonify({
                'title': info.get('title', 'Unknown Title'),
                'thumbnail': info.get('thumbnail', ''),
                'uploader': info.get('uploader', 'Unknown Channel'),
                'duration': duration_str
            })
    except Exception as e:
        return jsonify({'error': str(e)}), 400

def run_download_task(video_url, quality):
    def yt_progress_hook(d):
        if d['status'] == 'downloading':
            total_bytes = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
            downloaded_bytes = d.get('downloaded_bytes', 0)
            speed = d.get('speed', 0) or 0
            filename = d.get('filename', '')

            if total_bytes > 0:
                percent = round((downloaded_bytes / total_bytes) * 100, 1)
                downloaded_mb = round(downloaded_bytes / (1024 * 1024), 2)
                total_mb = round(total_bytes / (1024 * 1024), 2)
                speed_mb = round(speed / (1024 * 1024), 2)

                socketio.emit('progress', {
                    'percent': min(percent, 95.0),
                    'downloaded_mb': downloaded_mb,
                    'total_mb': total_mb,
                    'speed': f"{speed_mb} MB/s",
                    'stage': 'Downloading Stream...',
                    'filename': filename
                })

        elif d['status'] == 'finished':
            socketio.emit('progress', {
                'percent': 98.0,
                'downloaded_mb': 0,
                'total_mb': 0,
                'speed': '0 MB/s',
                'stage': 'Finalizing MP4 file...'
            })

    # Quality rules strictly for video formats
    if quality == '1080':
        format_rule = 'bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best'
    elif quality == '720':
        format_rule = 'bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best'
    else:
        format_rule = 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best'

    ydl_opts = {
        'format': format_rule,
        'merge_output_format': 'mp4',
        'ffmpeg_location': ffmpeg_dir,
        'outtmpl': os.path.join(DOWNLOAD_FOLDER, '%(title)s.%(ext)s'),
        'progress_hooks': [yt_progress_hook],
        'quiet': True,
        'extractor_args': {'youtube': {'player_client': ['web_safari', 'web']}}
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=True)
            video_title = info.get('title', 'video')
            clean_title = sanitize_filename(video_title)

        downloaded_file = os.path.join(DOWNLOAD_FOLDER, f"{clean_title}.mp4")

        if not os.path.exists(downloaded_file):
            mp4_files = [os.path.join(DOWNLOAD_FOLDER, f) for f in os.listdir(DOWNLOAD_FOLDER) if f.endswith('.mp4')]
            if mp4_files:
                downloaded_file = max(mp4_files, key=os.path.getmtime)

        if os.path.exists(downloaded_file):
            filename_only = os.path.basename(downloaded_file)
            socketio.emit('progress', {
                'percent': 100.0,
                'downloaded_mb': 0,
                'total_mb': 0,
                'speed': 'Done',
                'stage': 'Complete!'
            })
            socketio.emit('download_complete', {'file_url': f'/get-file/{filename_only}'})
        else:
            socketio.emit('download_error', {'error': 'Could not locate downloaded file.'})

    except Exception as e:
        socketio.emit('download_error', {'error': str(e)})

@socketio.on('start_download')
def handle_download(data):
    video_url = data.get('url')
    quality = data.get('quality', 'best')
    if not video_url:
        emit('download_error', {'error': 'URL is required'})
        return

    thread = threading.Thread(target=run_download_task, args=(video_url, quality))
    thread.daemon = True
    thread.start()

@app.route('/get-file/<path:filename>')
def get_file(filename):
    file_path = os.path.join(DOWNLOAD_FOLDER, filename)
    if os.path.exists(file_path):
        return send_file(file_path, as_attachment=True, download_name=filename)
    return jsonify({'error': 'File not found'}), 404

# 2. Dynamic Port & Host Binding for Production Deployment
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port, debug=False, use_reloader=False)
