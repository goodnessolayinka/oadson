# YouTube

## What It Does
Searches YouTube for videos, retrieves video metadata, and fetches transcripts where available. Returns view counts, upload dates, channel info, and direct links.

## When to Use It
- User says "find YouTube videos on", "search YouTube for", "get the transcript of"
- User wants video recommendations on a topic
- User wants to extract content from a YouTube video without watching it
- User researching a topic and wants video sources

## When NOT to Use It
- User wants to stream/play video inside OADSON — not supported
- User wants Spotify or music → use spotify skill
- User wants news → use news skill even if news is video-based

## Calling Convention
This is an `action`-based skill — call it with `action` set to one of:

| Action | What it does |
|---|---|
| `search` | find videos by keyword. |
| `video_info` | details/stats for a known video. |
| `transcript` | get a video's transcript. |
| `channel_videos` | recent videos from a named channel. |

## Output Format
```json
{
  "results": [
    {
      "title": "FastAPI Full Course for Beginners",
      "channel": "Traversy Media",
      "url": "https://www.youtube.com/watch?v=...",
      "duration": "1:42:30",
      "views": 1200000,
      "upload_date": "2025-08-14",
      "description_snippet": "In this course we will build..."
    }
  ],
  "transcript": "Welcome to this FastAPI tutorial..."
}
```

## Constraints
- Transcript availability: auto-generated or manually uploaded captions required
- Transcripts not available for all videos
- Search returns top 10 results by relevance
- Cannot download video files
- Age-restricted content returns metadata only (no transcript)
- Requires YOUTUBE_API_KEY in environment

## Examples
- "Find beginner CTF writeup videos"
- "Get the transcript of this conference talk"
- "What's the best Python MongoDB tutorial on YouTube?"
