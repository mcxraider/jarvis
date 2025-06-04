# Audio Conversion Setup

This application supports automatic conversion of various audio formats to MP3 for compatibility with the transcription service. To enable audio format conversion, you need to install FFmpeg.

## FFmpeg Installation

### macOS

#### Using Homebrew (Recommended)

```bash
# Install Homebrew if not already installed
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# Install FFmpeg
brew install ffmpeg
```

#### Using MacPorts

```bash
sudo port install ffmpeg
```

### Ubuntu/Debian

```bash
# Update package list
sudo apt update

# Install FFmpeg
sudo apt install ffmpeg

# Verify installation
ffmpeg -version
```

### CentOS/RHEL/Fedora

```bash
# For CentOS/RHEL (with EPEL repository)
sudo yum install epel-release
sudo yum install ffmpeg

# For Fedora
sudo dnf install ffmpeg

# Verify installation
ffmpeg -version
```

### Windows

#### Using Chocolatey (Recommended)

```powershell
# Install Chocolatey if not already installed
Set-ExecutionPolicy Bypass -Scope Process -Force; [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.ServicePointManager]::SecurityProtocol -bor 3072; iex ((New-Object System.Net.WebClient).DownloadString('https://community.chocolatey.org/install.ps1'))

# Install FFmpeg
choco install ffmpeg
```

#### Manual Installation

1. Download FFmpeg from [https://ffmpeg.org/download.html#build-windows](https://ffmpeg.org/download.html#build-windows)
2. Extract the archive to a folder (e.g., `C:\ffmpeg`)
3. Add the `bin` folder to your system PATH environment variable
4. Restart your command prompt/terminal

### Docker

If running in Docker, add FFmpeg to your Dockerfile:

```dockerfile
# For Ubuntu-based images
RUN apt-get update && apt-get install -y ffmpeg

# For Alpine-based images
RUN apk add --no-cache ffmpeg
```

## Supported Audio Formats

### Input Formats (Automatically Converted)

- `.oga` - Ogg Audio
- `.ogg` - Ogg Vorbis
- `.webm` - WebM Audio
- `.opus` - Opus Audio
- `.flac` - FLAC Audio
- `.aac` - AAC Audio
- `.wma` - Windows Media Audio
- `.amr` - Adaptive Multi-Rate

### Output Format

All input formats are converted to **MP3** with the following settings:

- **Codec**: libmp3lame
- **Bitrate**: 128kbps
- **Sample Rate**: 44.1kHz
- **Channels**: Stereo (2 channels)

### Natively Supported Formats (No Conversion Required)

- `.mp3` - MP3 Audio
- `.wav` - WAV Audio
- `.m4a` - MPEG-4 Audio

## Verification

To verify that FFmpeg is properly installed and available:

```bash
ffmpeg -version
```

You should see output showing the FFmpeg version and build information.

## Troubleshooting

### FFmpeg Not Found

If you get an error about FFmpeg not being available:

1. **Check Installation**: Verify FFmpeg is installed with `ffmpeg -version`
2. **Check PATH**: Ensure FFmpeg is in your system PATH
3. **Restart Application**: Restart the application after installing FFmpeg
4. **Permissions**: Ensure the application has permission to execute FFmpeg

### Conversion Failures

If audio conversion fails:

1. **Check Audio File**: Ensure the input audio file is not corrupted
2. **File Size**: Verify the audio file is under 25MB
3. **Format Support**: Check if the input format is in the supported list above
4. **Disk Space**: Ensure sufficient disk space for temporary files

### Performance Notes

- Conversion adds processing time (typically 2-10 seconds depending on file size)
- Temporary files are automatically cleaned up after conversion
- Conversion timeout is set to 30 seconds maximum

## Manual Conversion Alternative

If FFmpeg installation is not possible, you can manually convert audio files using online tools:

1. **Online Converters**:

   - [CloudConvert](https://cloudconvert.com/oga-to-mp3)
   - [OnlineAudioConverter](https://online-audio-converter.com/)
   - [Convertio](https://convertio.co/oga-mp3/)

2. **Desktop Applications**:
   - Audacity (Free, cross-platform)
   - VLC Media Player (Free, cross-platform)
   - iTunes/Music (macOS/Windows)

After manual conversion, send the MP3 file to the bot for transcription.
