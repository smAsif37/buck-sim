"""Single source of truth for test names and metric keys.

A metric key is ``<test>.<metric>``. Units/labels are shown in the UI and used
to validate pass/fail limits supplied by the user.
"""

from __future__ import annotations

TEST_NAMES: tuple[str, ...] = (
    "steady_state",
    "load_transient",
    "line_transient",
    "line_regulation",
    "load_regulation",
)

TEST_LABELS: dict[str, str] = {
    "steady_state": "Steady state",
    "load_transient": "Load transient",
    "line_transient": "Line transient",
    "line_regulation": "Line regulation",
    "load_regulation": "Load regulation",
}

# key -> (unit, label)
METRICS: dict[str, tuple[str, str]] = {
    # --- steady state (nominal Vin, full load) ---
    "steady_state.vout_mean_v": ("V", "Mean output voltage"),
    "steady_state.vout_error_pct": ("%", "Output voltage error vs. nominal"),
    "steady_state.ripple_pp_v": ("V", "Output ripple (peak-to-peak)"),
    "steady_state.ripple_rms_v": ("V", "Output ripple (RMS, AC part)"),
    "steady_state.iout_avg_a": ("A", "Mean load current"),
    "steady_state.iin_avg_a": ("A", "Mean input current"),
    "steady_state.pout_w": ("W", "Output power"),
    "steady_state.pin_w": ("W", "Input power"),
    "steady_state.efficiency_pct": ("%", "Efficiency"),
    # --- load transient (Iout_min -> Iout_max -> Iout_min) ---
    "load_transient.vout_pre_v": ("V", "Vout before load step (mean)"),
    "load_transient.up_peak_dev_v": ("V", "Load step up: peak deviation"),
    "load_transient.up_dev_pct": ("%", "Load step up: peak deviation (% of nominal)"),
    "load_transient.up_vout_min_v": ("V", "Load step up: minimum Vout"),
    "load_transient.up_vout_max_v": ("V", "Load step up: maximum Vout"),
    "load_transient.up_settle_us": ("us", "Load step up: settling time"),
    "load_transient.up_final_v": ("V", "Load step up: final Vout (mean)"),
    "load_transient.down_peak_dev_v": ("V", "Load step down: peak deviation"),
    "load_transient.down_dev_pct": ("%", "Load step down: peak deviation (% of nominal)"),
    "load_transient.down_vout_min_v": ("V", "Load step down: minimum Vout"),
    "load_transient.down_vout_max_v": ("V", "Load step down: maximum Vout"),
    "load_transient.down_settle_us": ("us", "Load step down: settling time"),
    "load_transient.down_final_v": ("V", "Load step down: final Vout (mean)"),
    # --- line transient (Vin_min -> Vin_max -> Vin_min) ---
    "line_transient.vout_pre_v": ("V", "Vout before line step (mean)"),
    "line_transient.up_peak_dev_v": ("V", "Vin step up: peak deviation"),
    "line_transient.up_dev_pct": ("%", "Vin step up: peak deviation (% of nominal)"),
    "line_transient.up_vout_min_v": ("V", "Vin step up: minimum Vout"),
    "line_transient.up_vout_max_v": ("V", "Vin step up: maximum Vout"),
    "line_transient.up_settle_us": ("us", "Vin step up: settling time"),
    "line_transient.up_final_v": ("V", "Vin step up: final Vout (mean)"),
    "line_transient.down_peak_dev_v": ("V", "Vin step down: peak deviation"),
    "line_transient.down_dev_pct": ("%", "Vin step down: peak deviation (% of nominal)"),
    "line_transient.down_vout_min_v": ("V", "Vin step down: minimum Vout"),
    "line_transient.down_vout_max_v": ("V", "Vin step down: maximum Vout"),
    "line_transient.down_settle_us": ("us", "Vin step down: settling time"),
    "line_transient.down_final_v": ("V", "Vin step down: final Vout (mean)"),
    # --- line regulation (Vin sweep at full load) ---
    "line_regulation.vout_min_v": ("V", "Minimum Vout over Vin sweep"),
    "line_regulation.vout_max_v": ("V", "Maximum Vout over Vin sweep"),
    "line_regulation.line_reg_pct": ("%", "Line regulation (total span, % of nominal)"),
    "line_regulation.line_reg_mv_per_v": ("mV/V", "Line regulation"),
    "line_regulation.efficiency_min_pct": ("%", "Minimum efficiency over Vin sweep"),
    "line_regulation.ripple_max_pp_v": ("V", "Maximum ripple over Vin sweep"),
    # --- load regulation (Iout sweep at nominal Vin) ---
    "load_regulation.vout_min_v": ("V", "Minimum Vout over load sweep"),
    "load_regulation.vout_max_v": ("V", "Maximum Vout over load sweep"),
    "load_regulation.load_reg_pct": ("%", "Load regulation (total span, % of nominal)"),
    "load_regulation.load_reg_mv_per_a": ("mV/A", "Load regulation"),
    "load_regulation.efficiency_peak_pct": ("%", "Peak efficiency over load sweep"),
    "load_regulation.efficiency_min_pct": ("%", "Minimum efficiency over load sweep"),
    "load_regulation.ripple_max_pp_v": ("V", "Maximum ripple over load sweep"),
}


def metric_unit(key: str) -> str:
    return METRICS[key][0]


def metric_label(key: str) -> str:
    return METRICS[key][1]
