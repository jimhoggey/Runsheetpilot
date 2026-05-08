"""Service Mate display constants and rule tables.

The clocks (GeekMagic SmallTV-Ultra) have a 240×240 RGB display. The render
helpers in render.py and the daemon in daemon.py both read from this
module — change values here and both layouts stay in sync.

Validated against adrienbrault/geekmagic-hacs (the Home Assistant integration).
Stock firmware HTTP surface used elsewhere in the package:
  POST /doUpload?dir=/image/  multipart, field "file"  → save image
  GET  /set?theme=3                                    → custom-image mode
  GET  /set?img=/image/<filename>                      → display
  GET  /set?brt=<1-100>                                → brightness
  GET  /app.json                                       → health
"""

# Display dimensions — Ultra is 240×240 RGB. Firmware (v9.0.39 confirmed)
# only renders JPG/GIF in Photo Album mode — PNG uploads succeed but the
# device won't display them. So we encode as JPEG.
SM_W, SM_H = 240, 240
SM_FILENAME = "rb_cue.jpg"           # uploaded under /image/ on the device
SM_TESTCARD_FILENAME = "rb_test.jpg"
SM_JPEG_QUALITY = 90
SM_ULTRA_IMAGE_THEME = 3   # Theme 3 = "Photo Album" (custom image full-screen)

# Daemon loop cadence — render every TICK; only POLL ProPresenter every Nth
# tick. 500 ms render lets the on-screen countdown step every 1 s instead of
# every 2 s; PP polling stays at 2 s so we don't hammer ProPresenter's API.
SM_LOOP_INTERVAL_S = 0.5
SM_PP_POLL_EVERY_N_TICKS = 4

# Per-verbosity font sizes — tweak here, layouts in render.py pick from these.
SM_FONTS = {
    "compact": {
        "label":   14,   # top role/type strip
        "title":   22,   # current item title
        "clock":   56,   # countdown
        "next":    13,   # "NEXT — TYPE" label
        "cue":     15,   # bottom cue band
    },
    "detailed": {
        "label":   12,
        "title":   18,
        "notes":   12,
        "clock":   42,
        "next":    12,
        "next_t":  14,   # next-item title (rendered, unlike compact)
        "then":    12,   # "then: <next-cue>" hint line
        "cue":     14,
    },
}
SM_VERBOSITY_DEFAULT = "compact"
SM_VERBOSITIES = ("compact", "detailed")

# Role accent colours used in the rendered cue images. Hex tuples (RGB).
ROLE_ACCENT = {
    "screen": (59, 130, 246),   # blue
    "sound":  (34, 197, 94),    # green
    "lights": (245, 158, 11),   # amber
}

# Fallback rule table for per-role cue text when the LLM doesn't supply one.
# Key = item type (matches the runsheet "type" field). Value = short imperative.
SCREEN_CUES = {
    "song":         "Cue song slides",
    "mc_on_stage":  "MC slide / lower-thirds",
    "sermon":       "Sermon slides",
    "scripture":    "Scripture slides",
    "announcement": "Announcement loop",
    "prayer":       "Prayer slide",
    "offering":     "Offering slide",
    "video":        "Video — full screen",
    "other":        "Stand by",
}
SOUND_CUES = {
    "song":         "Band mics live · MC mute",
    "mc_on_stage":  "MC mic ON · band mute",
    "sermon":       "Speaker mic ON",
    "scripture":    "Reader mic ON",
    "announcement": "MC mic ON",
    "prayer":       "Prayer mic ON",
    "offering":     "MC mic ON",
    "video":        "Video audio ON",
    "other":        "Stand by",
}
LIGHTS_CUES = {
    "song":         "Stage wash — band",
    "mc_on_stage":  "Spot — MC",
    "sermon":       "Spot — preacher",
    "scripture":    "Soft warm wash",
    "announcement": "House lights up",
    "prayer":       "Soft warm wash",
    "offering":     "House lights up",
    "video":        "Stage dim · screen up",
    "other":        "Stand by",
}
ROLE_CUE_TABLES = {
    "screen": SCREEN_CUES, "sound": SOUND_CUES, "lights": LIGHTS_CUES,
}
