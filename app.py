import os
import tempfile
import yt_dlp
from flask import Flask, render_template, request, jsonify, Response, send_file
import json
import time
import threading

app = Flask(__name__)
app.config['SECRET_KEY'] = 'your-secret-key-change-in-production'

# Store progress data per session (simple dict, use Redis in production)
progress_store = {}

def get_video_info(url):
    """Extract video info and available formats"""
    ydl_opts = {'quiet': True, 'no_warnings': True}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        formats = []
        for f in info.get('formats', []):
            # Filter out formats without video or audio (or combined)
            if f.get('vcodec') != 'none' and f.get('acodec') != 'none':
                # Combined format
                formats.append({
                    'format_id': f['format_id'],
                    'resolution': f.get('resolution', 'N/A'),
                    'fps': f.get('fps', 'N/A'),
                    'size_mb': round((f.get('filesize') or f.get('filesize_approx') or 0) / (1024*1024), 2),
                    'ext': f.get('ext', 'mp4'),
                    'type': 'video+audio'
                })
            elif f.get('vcodec') != 'none' and f.get('acodec') == 'none':
                # Video only
                formats.append({
                    'format_id': f['format_id'],
                    'resolution': f.get('resolution', 'N/A'),
                    'fps': f.get('fps', 'N/A'),
                    'size_mb': round((f.get('filesize') or f.get('filesize_approx') or 0) / (1024*1024), 2),
                    'ext': f.get('ext', 'mp4'),
                    'type': 'video only'
                })
        # Add audio-only formats
        audio_formats = []
        for f in info.get('formats', []):
            if f.get('acodec') != 'none' and f.get('vcodec') == 'none':
                audio_formats.append({
                    'format_id': f['format_id'],
                    'abr': f.get('abr', 'N/A'),
                    'size_mb': round((f.get('filesize') or f.get('filesize_approx') or 0) / (1024*1024), 2),
                    'ext': f.get('ext', 'm4a'),
                    'type': 'audio only'
                })
        return {
            'title': info.get('title', 'Unknown'),
            'duration': info.get('duration_string', 'Unknown'),
            'thumbnail': info.get('thumbnail', ''),
            'formats': formats,
            'audio_formats': audio_formats
        }

def progress_hook(d, download_id):
    """Callback for yt-dlp to update progress"""
    if d['status'] == 'downloading':
        total = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
        downloaded = d.get('downloaded_bytes', 0)
        if total:
            percent = int((downloaded / total) * 100)
            speed = d.get('speed', 0)
            speed_mb = speed / (1024*1024) if speed else 0
            progress_store[download_id] = {
                'percent': percent,
                'speed': f"{speed_mb:.2f} MB/s",
                'total': total,
                'downloaded': downloaded
            }
    elif d['status'] == 'finished':
        progress_store[download_id] = {'percent': 100, 'status': 'finished'}

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/get_formats', methods=['POST'])
def get_formats():
    """AJAX endpoint to fetch formats for a given URL"""
    data = request.get_json()
    url = data.get('url')
    if not url:
        return jsonify({'error': 'URL required'}), 400
    try:
        info = get_video_info(url)
        return jsonify(info)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/download', methods=['POST'])
def download():
    """Start download and return a download_id for progress tracking"""
    data = request.get_json()
    url = data.get('url')
    format_id = data.get('format_id', 'best')
    audio_only = data.get('audio_only', False)

    if not url:
        return jsonify({'error': 'URL required'}), 400

    # Generate unique ID for this download
    import uuid
    download_id = str(uuid.uuid4())
    progress_store[download_id] = {'percent': 0}

    # Start download in background thread
    thread = threading.Thread(target=perform_download, args=(url, format_id, audio_only, download_id))
    thread.daemon = True
    thread.start()

    return jsonify({'download_id': download_id})

def perform_download(url, format_id, audio_only, download_id):
    """Background download task"""
    temp_dir = tempfile.mkdtemp()
    output_template = os.path.join(temp_dir, '%(title)s.%(ext)s')

    ydl_opts = {
        'format': format_id,
        'outtmpl': output_template,
        'progress_hooks': [lambda d: progress_hook(d, download_id)],
        'quiet': True,
        'no_warnings': True,
        'merge_output_format': 'mp4',
    }

    if audio_only:
        ydl_opts['format'] = 'bestaudio/best'
        ydl_opts['postprocessors'] = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }]
        ydl_opts['outtmpl'] = os.path.join(temp_dir, '%(title)s.%(ext)s')

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            # Get the downloaded file path
            downloaded_file = ydl.prepare_filename(info)
            if audio_only:
                # For audio, the file will have .mp3 extension
                base = os.path.splitext(downloaded_file)[0]
                downloaded_file = base + '.mp3' if not downloaded_file.endswith('.mp3') else downloaded_file

            # Store the file path in progress store for streaming
            progress_store[download_id]['file_path'] = downloaded_file
            progress_store[download_id]['filename'] = os.path.basename(downloaded_file)
            progress_store[download_id]['status'] = 'completed'
    except Exception as e:
        progress_store[download_id]['status'] = 'error'
        progress_store[download_id]['error'] = str(e)

@app.route('/progress/<download_id>')
def progress_stream(download_id):
    """SSE endpoint for real‑time progress updates"""
    def generate():
        last_percent = -1
        while True:
            data = progress_store.get(download_id, {})
            percent = data.get('percent', 0)
            if percent != last_percent:
                last_percent = percent
                yield f"data: {json.dumps(data)}\n\n"
            if data.get('status') in ('completed', 'error'):
                # Send final event
                yield f"data: {json.dumps(data)}\n\n"
                break
            time.sleep(0.5)
    return Response(generate(), mimetype='text/event-stream')

@app.route('/download_file/<download_id>')
def download_file(download_id):
    """Serve the downloaded file and delete it afterward"""
    data = progress_store.get(download_id, {})
    file_path = data.get('file_path')
    if not file_path or not os.path.exists(file_path):
        return "File not found", 404

    filename = data.get('filename', 'video.mp4')
    return send_file(file_path, as_attachment=True, download_name=filename)

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)