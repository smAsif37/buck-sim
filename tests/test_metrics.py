import numpy as np
import pytest

from buck_sim import metrics as m

T = 2e-6  # 500 kHz


def grid(t_end=400e-6, dt=T / 100):
    return np.arange(0, t_end + dt / 2, dt)


def test_time_mean_and_window_endpoints():
    t = grid()
    y = np.full_like(t, 3.0)
    assert m.time_mean(t, y, 10e-6, 50e-6) == pytest.approx(3.0)
    ramp = 1e6 * t  # 1 V per us
    # mean of a ramp over [10us, 30us] is the value at 20us
    assert m.time_mean(t, ramp, 10e-6, 30e-6) == pytest.approx(20.0, rel=1e-6)


def test_window_outside_range_raises():
    t = grid(100e-6)
    with pytest.raises(m.MeasurementError):
        m.time_mean(t, np.zeros_like(t), 50e-6, 150e-6)
    with pytest.raises(m.MeasurementError):
        m.time_mean(t, np.zeros_like(t), 50e-6, 50e-6)


def test_ripple_measurements_on_sine():
    t = grid()
    amp = 0.02
    y = 5.0 + amp * np.sin(2 * np.pi * t / T)
    t0, t1 = 100e-6, 100e-6 + 50 * T
    assert m.time_mean(t, y, t0, t1) == pytest.approx(5.0, abs=1e-4)
    assert m.peak_to_peak(t, y, t0, t1) == pytest.approx(2 * amp, rel=0.01)
    assert m.time_rms_ac(t, y, t0, t1) == pytest.approx(amp / np.sqrt(2), rel=0.01)


def test_cycle_average_removes_ripple():
    t = grid()
    y = 5.0 + 0.05 * np.sin(2 * np.pi * t / T)
    tc, yc = m.cycle_average(t, y, T)
    assert np.max(np.abs(yc - 5.0)) < 0.003
    assert tc[0] > t[0] and tc[-1] < t[-1]


def test_uniform_resample_bounds():
    t = np.array([0.0, 1.0, 3.0])
    y = np.array([0.0, 1.0, 3.0])
    g, v = m.uniform_resample(t, y, 0.5)
    assert g[0] == 0.0 and g[-1] == pytest.approx(3.0)
    assert v == pytest.approx(g)


def test_step_metrics_first_order_recovery():
    t = grid(600e-6)
    t0, tau = 200e-6, 20e-6
    y = np.where(t < t0, 5.0, 5.0 - 0.2 * np.exp(-(t - t0) / tau))
    y = y + 0.01 * np.sin(2 * np.pi * t / T)  # ripple must not upset the result
    res = m.step_metrics(
        t, y, period=T, t_event=t0,
        pre_window=(t0 - 50 * T, t0), final_window=(600e-6 - 50 * T, 600e-6),
        t_end=600e-6, band_abs=0.01,
    )  # fmt: skip
    assert res["pre"] == pytest.approx(5.0, abs=2e-3)
    assert res["final"] == pytest.approx(5.0, abs=2e-3)
    assert res["peak_dev"] == pytest.approx(0.2, abs=0.02)
    # 0.2*exp(-x/tau) = 0.01  ->  x = tau*ln(20) = 59.9 us
    assert res["settle_s"] == pytest.approx(tau * np.log(20), abs=4e-6)
    assert res["raw_min"] < 4.82


def test_step_metrics_reports_unsettled_as_none():
    t = grid(300e-6)
    t0 = 100e-6
    y = np.where(t < t0, 5.0, 5.0 + 0.5 * (t - t0) / 200e-6)  # still drifting at the end
    res = m.step_metrics(
        t, y, period=T, t_event=t0,
        pre_window=(t0 - 20 * T, t0), final_window=(300e-6 - 20 * T, 300e-6),
        t_end=300e-6, band_abs=0.01,
    )  # fmt: skip
    assert res["settle_s"] is None


def test_step_metrics_no_excursion_gives_zero_settle():
    t = grid(300e-6)
    y = np.full_like(t, 5.0)
    res = m.step_metrics(
        t, y, period=T, t_event=100e-6,
        pre_window=(60e-6, 100e-6), final_window=(250e-6, 290e-6),
        t_end=290e-6, band_abs=0.05,
    )  # fmt: skip
    assert res["settle_s"] == 0.0
    assert res["peak_dev"] == pytest.approx(0.0, abs=1e-9)


def test_cycle_average_needs_enough_data():
    t = np.linspace(0, 0.5 * T, 50)
    with pytest.raises(m.MeasurementError):
        m.cycle_average(t, np.zeros_like(t), T)
