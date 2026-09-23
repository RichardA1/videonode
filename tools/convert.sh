#!/usr/bin/env bash
# Batch-convert videos into Pi-friendly MP4s (Linux/macOS version of convert.ps1).
#
#   bash tools/convert.sh [-h 720|1080] [-b border.png] [-i inset%] [-q crf] [-f] INPUT_DIR OUTPUT_DIR
#
#   -h  output height, 720 (default) or 1080
#   -b  PNG with a transparent middle, drawn over the video as a frame
#   -i  shrink the video by this percent on each side to sit inside the border
#   -q  quality, lower = better/bigger (default 21)
#   -f  re-convert files that already exist in OUTPUT_DIR
set -euo pipefail

HEIGHT=720 BORDER="" INSET=0 CRF=21 FORCE=0
while getopts "h:b:i:q:f" opt; do
    case $opt in
        h) HEIGHT=$OPTARG ;;
        b) BORDER=$OPTARG ;;
        i) INSET=$OPTARG ;;
        q) CRF=$OPTARG ;;
        f) FORCE=1 ;;
        *) sed -n '2,11p' "$0"; exit 1 ;;
    esac
done
shift $((OPTIND - 1))
[[ $# -eq 2 ]] || { sed -n '2,11p' "$0"; exit 1; }
IN=$1 OUT=$2

command -v ffmpeg >/dev/null || { echo "ffmpeg not found"; exit 1; }
[[ -z $BORDER || -f $BORDER ]] || { echo "Border image not found: $BORDER"; exit 1; }
case $HEIGHT in
    720)  W=1280 MAXRATE=4M BUF=8M  LEVEL=4.0 ;;
    1080) W=1920 MAXRATE=8M BUF=16M LEVEL=4.1 ;;
    *) echo "Height must be 720 or 1080"; exit 1 ;;
esac
H=$HEIGHT
IW=$(( W * (100 - 2 * INSET) / 200 * 2 ))
IH=$(( H * (100 - 2 * INSET) / 200 * 2 ))
mkdir -p "$OUT"

ok=0 failed=()
shopt -s nullglob nocaseglob
files=("$IN"/*.{mp4,mkv,mov,avi,m4v,webm,wmv,flv,ts,mpg,mpeg})
[[ ${#files[@]} -gt 0 ]] || { echo "No videos found in $IN"; exit 0; }

n=0
for f in "${files[@]}"; do
    n=$((n + 1))
    name=$(basename "$f"); out="$OUT/${name%.*}.mp4"
    if [[ -e $out && $FORCE -eq 0 ]]; then
        echo "[$n/${#files[@]}] Skipping $name (already converted)"; continue
    fi
    echo "[$n/${#files[@]}] Converting $name"

    # Halve 50/60 fps sources; the Pi 3 can't keep up with them.
    fps=""
    rate=$(ffprobe -v error -select_streams v:0 -show_entries stream=avg_frame_rate -of default=nw=1:nk=1 "$f" | head -1)
    if [[ $rate =~ ^([0-9]+)/([0-9]+)$ ]] && (( BASH_REMATCH[2] > 0 )) \
            && (( BASH_REMATCH[1] > 31 * BASH_REMATCH[2] )); then
        fps=",fps=${BASH_REMATCH[1]}/$(( 2 * BASH_REMATCH[2] ))"
    fi
    fit="scale=$IW:$IH:force_original_aspect_ratio=decrease,pad=$W:$H:(ow-iw)/2:(oh-ih)/2:black,setsar=1$fps"

    args=(-hide_banner -loglevel error -stats -y -i "$f")
    if [[ -n $BORDER ]]; then
        args+=(-loop 1 -i "$BORDER" -filter_complex
               "[0:v]${fit}[v];[1:v]scale=${W}:${H}[b];[v][b]overlay=0:0:shortest=1,format=yuv420p[out]"
               -map "[out]")
    else
        args+=(-vf "$fit,format=yuv420p" -map 0:v:0)
    fi
    args+=(-map "0:a:0?" -c:v libx264 -preset slow -crf "$CRF" -profile:v high -level:v "$LEVEL"
           -maxrate "$MAXRATE" -bufsize "$BUF" -c:a aac -b:a 160k -ac 2 -movflags +faststart "$out")

    if ffmpeg "${args[@]}" < /dev/null; then
        ok=$((ok + 1))
    else
        failed+=("$name"); rm -f "$out"; echo "!! Failed: $name"
    fi
done

echo; echo "Converted $ok file(s) to $OUT"
[[ ${#failed[@]} -eq 0 ]] || echo "Failed: ${failed[*]}"
