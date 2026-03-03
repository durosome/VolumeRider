# VolumeRider
OBS Python Script for Automatic Volume Adjustment
VolumeRider is a script for OBS Studio that automatically adjusts the volume of one audio source based on the loudness (LUFS) of another source. It helps maintain a consistent level or keeping a voice track at a stable level.

Features
Real‑time loudness measurement – uses OBS’s internal volmeter to obtain dBFS levels and computes an approximate short‑term LUFS value with exponential smoothing.

Adjustable target level – set the desired LUFS value for the listen source.

Freeze on silence – a threshold (in dB) prevents the smoothed level from updating when the instantaneous level drops below it, so the volume doesn’t “fly away” during pauses.

Hold mode – manually freeze the measurement.

Two modes:

Fast – updates every timer tick, smoothing over ~5 seconds.

Slow – updates every 4th tick, smoothing over ~10 seconds (even smoother).

Bypass – temporarily disables automatic adjustment and resets the controlled source’s volume to 1.0 (saving the previous value to restore later).

Debug logging – prints detailed information to the OBS log for tuning.

Installation
Save the script file (e.g. vol_ride_4.py) to a folder of your choice.

In OBS Studio, go to Tools → Scripts.

Click the + (plus) button and select the script file.

The script will appear in the list; configure the settings as described below.

Settings
Parameter	Description
Target LUFS	The desired loudness level for the listen source (in dB LUFS). Typical values: -18 … -23 for speech, -14 … -16 for music.
Threshold (dB)	If the instantaneous level of the listen source falls below this value, the smoothed LUFS is frozen (prevents volume runaway during silence). Set it a few dB above your background noise floor.
Attack mode	Fast (5 sec) – updates every 500 ms, smoothing over ~5 seconds.
Slow (10 sec) – updates every 2 seconds, smoothing over ~10 seconds. Slower gives more stable readings but reacts slower to changes.
Hold	Manually freeze the LUFS measurement – useful for debugging or when you want to temporarily stop adjustments.
Bypass	Completely disables volume changes. The controlled source’s volume is set to 1.0; when bypass is turned off, the previous volume is restored.
Debug logging	When enabled, the script prints detailed information (current LUFS, target, delta, gain) and freeze/resume events to the OBS log.
How It Works
The script creates a permanent volmeter for the listen source and continuously receives its instantaneous dBFS level.

A smoothed LUFS value is calculated using an exponential moving average (EMA). The smoothing period depends on the chosen attack mode.

If the instantaneous level falls below the threshold (or Hold is enabled), the smoothed LUFS is frozen – it does not update.

The difference between the target LUFS and the current smoothed value is converted to a linear gain factor (10^(delta/20)).

The gain is applied smoothly to the control source using a low‑pass filter to avoid abrupt changes.

The adjustment runs every 500 ms (or every 2 seconds in Slow mode) to keep the volume aligned with the target.

Tips for Best Results
Set the threshold carefully – it should be high enough to catch real silences (e.g. between songs or sentences) but low enough not to trigger on every micro‑pause. A value of -50…-60 dB often works well for speech with a quiet background.

Choose the attack mode – Fast is suitable for speech that varies quickly; Slow gives a more stable reading and gentler volume changes, ideal for music.

Use debug logging initially to observe the behaviour and fine‑tune your settings.

If you experience feedback (the controlled source affecting the listen source), ensure the two sources are different.

License
This script is provided under the MIT License. Feel free to modify and share.

Note: The script uses ctypes to access OBS internals. It requires OBS Studio 27 or later with Python scripting support enabled. If you encounter issues, check the OBS log for error messages.
