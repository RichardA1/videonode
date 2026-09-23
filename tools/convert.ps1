<#
.SYNOPSIS
    Batch-convert videos into MP4s that play smoothly on a Raspberry Pi,
    optionally burning in a themed border/frame.

.DESCRIPTION
    Output: H.264 (8-bit, yuv420p) + AAC, 720p or 1080p, max 30 fps, capped
    bitrate. Videos over 30 fps are halved (60 -> 30, 50 -> 25). Existing
    output files are skipped unless -Force is given.

    Requires ffmpeg on your PATH:  winget install Gyan.FFmpeg

.PARAMETER Border
    A PNG the same shape as the output (16:9), with a transparent middle.
    It's scaled to the output size and drawn on top of the video.

.PARAMETER Inset
    Shrinks the video by this percent on each side so it sits inside the
    border instead of underneath it (e.g. 6 for a thin frame).

.EXAMPLE
    .\convert.ps1 -InputDir C:\Videos\raw -OutputDir C:\Videos\pi

.EXAMPLE
    .\convert.ps1 -InputDir raw -OutputDir pi -Height 1080 -Border frame.png -Inset 6
#>
param(
    [Parameter(Mandatory)][string]$InputDir,
    [Parameter(Mandatory)][string]$OutputDir,
    [ValidateSet(720, 1080)][int]$Height = 720,
    [string]$Border,
    [ValidateRange(0, 30)][int]$Inset = 0,
    [ValidateRange(15, 35)][int]$Crf = 21,
    [switch]$Force
)
$ErrorActionPreference = 'Stop'

foreach ($tool in 'ffmpeg', 'ffprobe') {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        throw "$tool not found. Install FFmpeg (winget install Gyan.FFmpeg) and open a new PowerShell window."
    }
}
if ($Border -and -not (Test-Path $Border)) { throw "Border image not found: $Border" }

$W = if ($Height -eq 1080) { 1920 } else { 1280 }
$H = $Height
$MaxRate = if ($Height -eq 1080) { '8M' } else { '4M' }
$BufSize = if ($Height -eq 1080) { '16M' } else { '8M' }
$Level = if ($Height -eq 1080) { '4.1' } else { '4.0' }

# Size of the video area inside the border (even numbers for H.264)
$IW = [int]([math]::Floor($W * (100 - 2 * $Inset) / 200) * 2)
$IH = [int]([math]::Floor($H * (100 - 2 * $Inset) / 200) * 2)

New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
$exts = '.mp4', '.mkv', '.mov', '.avi', '.m4v', '.webm', '.wmv', '.flv', '.ts', '.mpg', '.mpeg'
$files = @(Get-ChildItem -LiteralPath $InputDir -File | Where-Object { $exts -contains $_.Extension.ToLower() })
if ($files.Count -eq 0) { Write-Warning "No videos found in $InputDir"; return }

$i = 0; $ok = 0; $failed = @()
foreach ($f in $files) {
    $i++
    $out = Join-Path $OutputDir ($f.BaseName + '.mp4')
    if ((Test-Path -LiteralPath $out) -and -not $Force) {
        Write-Host "[$i/$($files.Count)] Skipping $($f.Name) (already converted)" -ForegroundColor DarkGray
        continue
    }
    Write-Host "[$i/$($files.Count)] Converting $($f.Name)" -ForegroundColor Cyan

    # Halve the frame rate of 50/60 fps sources; the Pi 3 can't keep up with them.
    $fpsFilter = ''
    $rate = (& ffprobe -v error -select_streams v:0 -show_entries stream=avg_frame_rate `
                -of default=nw=1:nk=1 $f.FullName | Select-Object -First 1)
    if ($rate -match '^(\d+)/(\d+)$' -and [int]$Matches[2] -gt 0) {
        $num = [int64]$Matches[1]; $den = [int64]$Matches[2]
        if ($num / $den -gt 31) { $fpsFilter = ",fps=$num/$(2 * $den)" }
    }

    $fit = "scale=${IW}:${IH}:force_original_aspect_ratio=decrease," +
           "pad=${W}:${H}:(ow-iw)/2:(oh-ih)/2:black,setsar=1$fpsFilter"

    $ffArgs = @('-hide_banner', '-loglevel', 'error', '-stats', '-y', '-i', $f.FullName)
    if ($Border) {
        $ffArgs += @('-loop', '1', '-i', $Border,
                   '-filter_complex', "[0:v]$fit[v];[1:v]scale=${W}:${H}[b];[v][b]overlay=0:0:shortest=1,format=yuv420p[out]",
                   '-map', '[out]')
    } else {
        $ffArgs += @('-vf', "$fit,format=yuv420p", '-map', '0:v:0')
    }
    $ffArgs += @('-map', '0:a:0?',
               '-c:v', 'libx264', '-preset', 'slow', '-crf', $Crf,
               '-profile:v', 'high', '-level:v', $Level,
               '-maxrate', $MaxRate, '-bufsize', $BufSize,
               '-c:a', 'aac', '-b:a', '160k', '-ac', '2',
               '-movflags', '+faststart', $out)

    & ffmpeg @ffArgs
    if ($LASTEXITCODE -eq 0) {
        $ok++
    } else {
        $failed += $f.Name
        Remove-Item -LiteralPath $out -ErrorAction SilentlyContinue
        Write-Warning "Failed: $($f.Name)"
    }
}

Write-Host ""
Write-Host "Converted $ok file(s) to $OutputDir" -ForegroundColor Green
if ($failed) { Write-Warning ("Failed: " + ($failed -join ', ')) }
