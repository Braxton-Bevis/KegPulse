# Calibration and verification

KegPulse converts immutable raw pulse counts to milliliters with a versioned calibration. No
universal sensor K-factor is assumed. Calibrate on the water rig first, repeat on the installed
keg and final line, and periodically verify with a weighed pour.

KegPulse is a personal monitoring tool, not a legal-for-trade meter.

## What to prepare

- A stable scale with suitable capacity and resolution.
- A dry collection glass or vessel that can be tared.
- The representative water rig described in [HARDWARE.md](HARDWARE.md).
- Ten varied pours rather than ten identical pours.
- The liquid density in grams per milliliter. Water defaults to `1.000 g/mL`; use a known beer
  density when available, or clearly record that an entered value is approximate.

Density matters directly. Mass is not treated as volume without it.

## Ten-pour calibration

1. Open **Calibration & verification** and create a calibration run for the liquid and density.
2. Tare the empty collection glass on the scale.
3. Select **Capture sample 1**, make a pour, and wait for the flow state to complete.
4. Read the scale mass, enter it with the density used for that sample, and save it.
5. Empty/dry or retare the vessel as appropriate, then repeat for all ten samples. Include a
   useful range of small and larger pours within the meter's intended flow conditions.
6. Review all raw pulses, scale volumes, residuals, and consistency flags.
7. Explicitly include or exclude suspected outliers. KegPulse never removes one automatically;
   at least seven of the ten stored samples must remain included.
8. Review the aggregate factor and activate it. Activation versions the calibration; it does
   not rewrite old pours.

Each sample permanently stores raw pulses, mass, density, derived milliliters, capture time,
inclusion choice, and the consistency flag.

## Formulas

For each sample `i`:

```text
scale_volume_i_mL = mass_i_g / density_i_g_per_mL
```

The active aggregate estimator is:

```text
pulses_per_mL = sum(included raw_pulses_i)
                / sum(included scale_volume_i_mL)
```

It is intentionally not the arithmetic mean of individual pulse/volume ratios. A normal pour
uses the calibration captured for that pour:

```text
predicted_volume_mL = raw_pulses / pulses_per_mL
```

Calculations use decimal arithmetic and round only for display.

## Outlier and input rules

KegPulse evaluates individual `pulses / scale_volume` ratios with a median absolute deviation
(MAD) rule. With nonzero MAD, a sample is flagged when its modified z-score exceeds `3.5`. If
MAD is zero, any unequal ratio is flagged. The flag is a review aid, not an automatic exclusion.

The service rejects non-finite, zero, negative, implausibly tiny, or oversized mass/density
values and zero pulse samples. API/UI bounds are the executable source of truth. Correct a data
entry by reviewing the draft run; never guess a missing value.

Potential causes of inconsistency include scale error, incomplete taring, trapped air, changing
flow conditions, sensor orientation, loose wiring, pulse noise, tubing restrictions, or an
incorrect density. Investigate the physical cause before activating a suspicious factor.

## Periodic weighed verification

1. With an active calibration, select **Start weighed verification pour**.
2. Tare the vessel, make one representative pour, and wait for capture to complete.
3. Enter the measured mass and density.
4. Compare predicted volume, scale-derived volume, absolute error, and percentage error.

```text
absolute_error_mL = abs(predicted_volume_mL - scale_volume_mL)
percentage_error = absolute_error_mL / scale_volume_mL * 100
```

The default drift warning threshold is 5%, configurable in settings. A warning is stored with
the verification record and never changes the active factor automatically. If drift appears,
inspect the sensor, flow conditions, wiring, tubing, density, and calibration, then create a new
ten-pour version if necessary.

The percentage error denominator is the scale-derived volume. A zero or invalid scale volume is
rejected instead of producing an infinite or invented result.

## Calibration lifecycle and history

- A draft becomes active only after ten samples exist and at least seven are included.
- Activating a new version supersedes the prior active version without deleting it.
- A pour retains its original raw pulses, keg ID, calibration ID, and computed volume.
- Calibration and verification capture sessions do not decrement keg inventory or appear as
  beverage pours.
- Ordinary pulses recorded before a calibration remain as raw `needs_review` evidence with
  unknown volume; a later calibration does not silently reinterpret them.

After the water-rig run, repeat the full calibration under the installed keg and line conditions.
Record the physical setup and verification result with the hardware acceptance evidence.
