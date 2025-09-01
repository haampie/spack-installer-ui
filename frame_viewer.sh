#!/bin/bash
# Frame viewer script
# Usage: ./frame_viewer.sh [frames_directory] [starting_frame_number]

FRAMES_DIR="${1:-/tmp/frames}"
CURRENT="${2:-1}"

if [[ ! -d "$FRAMES_DIR" ]]; then
    echo "Frames directory $FRAMES_DIR not found"
    exit 1
fi

# Function to display frame
show_frame() {
    local frame_num="$1"
    local file=$(printf "%s/%04d.txt" "$FRAMES_DIR" "$frame_num")
    
    clear
    if [[ -f "$file" ]]; then
        echo "Frame $frame_num (←/→ navigate, q=quit, j/k for +/-10):"
        echo "======================================================="
        cat "$file"
        echo "======================================================="
        return 0
    else
        echo "Frame $frame_num not found"
        echo "Available frames: $(ls "$FRAMES_DIR"/*.txt 2>/dev/null | wc -l)"
        return 1
    fi
}

# Main loop
while true; do
    show_frame "$CURRENT"
    
    # Read single character
    read -n1 -s key
    
    case "$key" in
        $'\x1b')  # ESC sequence
            read -n2 -s rest
            case "$rest" in
                '[C') ((CURRENT++)) ;;  # Right arrow
                '[D') ((CURRENT > 1)) && ((CURRENT--)) ;;  # Left arrow
            esac
            ;;
        'j') ((CURRENT += 10)) ;;  # Jump forward 10
        'k') ((CURRENT > 10)) && ((CURRENT -= 10)) || CURRENT=1 ;;  # Jump back 10
        'n') ((CURRENT++)) ;;
        'p') ((CURRENT > 1)) && ((CURRENT--)) ;;
        'q') break ;;
        ' ') ((CURRENT++)) ;;  # Space for next
    esac
done

clear
echo "Frame viewer exited"
