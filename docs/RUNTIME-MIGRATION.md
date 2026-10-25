# Debian 13 / FFmpeg 7 migration

Native installs require Python **3.12.4 or newer** because reolinkapi >=0.4.1 requires it. The Debian 13 image uses Python 3.13 and FFmpeg 7.1. Container CI builds the image and checks Python dependencies, Node/sidecar loading, Mesa and encoder availability; it does not establish hardware compatibility.

Some Reolink firmware has audio timestamps that cause copied AAC to delay video in FFmpeg 7 FLV output until Protect times out. Before upgrading a passthrough deployment, use:

```text
--ffmpeg-args "-c:v copy -ar 16000 -ac 1 -c:a aac -b:a 32k"
```

This copies video and re-encodes audio on the CPU; it does not enable GPU encoding. Test live video, recordings and audio with the camera before wider rollout. Existing custom arguments are not silently rewritten.

For lower-channel hardware transcoding, PR #6 adds `--lo-audio-args "-ar 16000 -ac 1 -c:a aac -b:a 32k"`. Supply it with `--lo-h265-transcode`; it does not affect passthrough or the high channel. Use the global arguments above for copied-video channels. `--no-audio` from #6 disables audio entirely and is a fallback, not the normal migration setting.

The image includes Intel iHD and AMD Mesa drivers. Driver selection is automatic: neither the image nor proxy forces iHD. Use `--vaapi-driver iHD` for an explicit Intel override or `--vaapi-driver radeonsi` for AMD, only when required. An existing `LIBVA_DRIVER_NAME` environment override is respected; the flag overrides it only in streaming subprocesses. `--hw-backend qsv` selects Intel QSV; `--hw-backend vaapi` supports AMD with Mesa and an accessible render device. Backend/driver selection alone does not enable encoding; the transcode flags do. See [AMD VA-API](AMD-VAAPI.md).
