import pandas as pd
import pytest
from tv_history.timestamp_profiles import profile_date, served_profile
from test_trading_dates import frame


@pytest.mark.parametrize('profile,raw,date', [
    ('cme_overnight','2026-03-01T23:00Z','2026-03-02'),
    ('cme_overnight','2026-03-08T22:00Z','2026-03-09'),
    ('ice_brent','2026-03-01T23:00Z','2026-03-02'),
    ('ice_brent','2026-03-03T01:00Z','2026-03-03'),
    ('utc_calendar','2026-03-01T00:00Z','2026-03-01'),
    ('cme_overnight','1983-07-31T21:00Z','1983-08-01'),
    ('cme_overnight','2026-03-02T21:59Z','2026-03-02'),
    ('cme_overnight','2026-03-02T22:00Z','2026-03-03'),
    ('cme_overnight','2026-03-03T05:59Z','2026-03-03'),
    ('cme_overnight','2026-03-03T06:00Z','2026-03-03'),
    ('ice_brent','2026-03-02T21:59Z','2026-03-02'),
    ('ice_brent','2026-03-02T22:00Z','2026-03-03'),
    ('ice_brent','2026-07-05T21:30Z','2026-07-06'),
    ('moex_futures','2026-03-02T14:59Z','2026-03-02'),
    ('moex_futures','2026-03-02T15:00Z','2026-03-03'),
    ('moex_futures','2026-09-05T06:00Z','2026-09-07'),
])
def test_profile_dst_and_same_day(profile, raw, date):
    assert profile_date(pd.Timestamp(raw), profile) == pd.Timestamp(date, tz='UTC')


def test_legacy_sunday_weekly_evening_uses_next_monday_without_daily_backup():
    weekly = frame(['1983-07-31T21:00Z', '1983-08-07T21:00Z'])
    empty = weekly.iloc[:0]
    result = served_profile(weekly, empty, empty, empty, pd.Timestamp('1983-08-09T00:00Z'), '1W', 'cme_overnight')
    assert list(result.index) == [pd.Timestamp('1983-08-01T00:00Z'), pd.Timestamp('1983-08-08T00:00Z')]
    assert len(result) == len(weekly)
    assert result.attrs['timestamp_normalization']['unrecognized_source_timestamps'] == []


def test_unverified_holiday_bar_retains_standard_date_fallback():
    daily = frame(['2026-07-02T22:00Z','2026-07-06T22:00Z'])
    empty = daily.iloc[:0]
    result = served_profile(daily, daily, empty, empty, pd.Timestamp('2026-07-06T00:00Z'), '1D', 'cme_overnight')
    assert list(result.index) == [pd.Timestamp('2026-07-03T00:00Z')]
    assert result.attrs['timestamp_normalization']['unverified_rows'] == 1


def test_verified_overnight_bar_preserves_prices_and_moves_date():
    daily = frame(['2026-03-01T23:00Z','2026-03-02T23:00Z'])
    four = frame(['2026-03-01T23:00Z','2026-03-02T03:00Z','2026-03-02T07:00Z',
                  '2026-03-02T11:00Z','2026-03-02T15:00Z','2026-03-02T19:00Z','2026-03-02T23:00Z'])
    result = served_profile(daily, daily, four, four.iloc[:0], pd.Timestamp('2026-03-03T00:00Z'), '1D', 'cme_overnight')
    assert list(result.index) == [pd.Timestamp('2026-03-02T00:00Z'), pd.Timestamp('2026-03-03T00:00Z')]
    assert result.iloc[0][daily.columns].tolist() == daily.iloc[0].tolist()


def test_brent_evening_four_hour_bar_keeps_london_trading_date():
    daily = frame(['2026-03-03T01:00Z', '2026-03-04T01:00Z'])
    four = frame(['2026-03-03T01:00Z', '2026-03-03T05:00Z',
                  '2026-03-03T09:00Z', '2026-03-03T13:00Z',
                  '2026-03-03T17:00Z', '2026-03-03T21:00Z', '2026-03-04T01:00Z'])
    result = served_profile(daily, daily, four, four.iloc[:0], pd.Timestamp('2026-03-05T00:00Z'), '1D', 'ice_brent')
    assert result.index[0] == pd.Timestamp('2026-03-03T00:00Z')
    assert result.iloc[0].timestamp_basis == 'four_hour_confirmed'


def test_bitcoin_calendar_does_not_require_intraday_cache():
    daily = frame(['2026-03-01T00:00Z','2026-03-02T00:00Z'])
    result = served_profile(daily, daily, daily.iloc[:0], daily.iloc[:0], pd.Timestamp('2026-03-02T00:00Z'), '1D', 'utc_calendar')
    assert result.index.equals(daily.index)


def test_weekly_ohl_mismatch_is_reported_and_preserved():
    daily = frame(['2026-03-02T00:00Z','2026-03-09T00:00Z'])
    weekly = daily.copy()
    weekly.iloc[0, 1] = 99
    result = served_profile(weekly, daily, daily.iloc[:0], daily.iloc[:0], pd.Timestamp('2026-03-09T00:00Z'), '1W', 'utc_calendar')
    assert len(result) == 2
    assert result.iloc[0].high == 99
    assert result.attrs['timestamp_normalization']['unverified_rows'] == 2


@pytest.mark.parametrize('defect', [None, 'missing_bar', 'bad_high', 'daytime_start'])
@pytest.mark.parametrize('profile', ['cme_overnight', 'ice_brent'])
def test_cme_holiday_overnight_tail_uses_ohl_and_preserves_volume(defect, profile):
    daily = frame(['2026-06-18T22:00Z', '2026-06-22T22:00Z'])
    daily.iloc[0, 4] = 9999
    stamps = ['2026-06-18T22:00Z','2026-06-19T02:00Z',
              '2026-06-21T22:00Z','2026-06-22T02:00Z','2026-06-22T06:00Z',
              '2026-06-22T10:00Z','2026-06-22T14:00Z','2026-06-22T18:00Z','2026-06-22T22:00Z']
    four = frame(stamps)
    four.iloc[0, 0] = 20
    if defect == 'missing_bar': four = four.drop(pd.Timestamp('2026-06-22T06:00Z'))
    if defect == 'bad_high': daily.iloc[0, 1] = 99
    if defect == 'daytime_start':
        four = four.drop([pd.Timestamp('2026-06-21T22:00Z'), pd.Timestamp('2026-06-22T02:00Z')])
    cutoff = pd.Timestamp('2026-06-22T00:00Z')
    view = served_profile(daily,daily,four,four.iloc[:0],cutoff,'1D',profile)
    expected = '2026-06-22' if defect is None else '2026-06-19'
    assert view.index[0] == pd.Timestamp(expected,tz='UTC')
    assert view.iloc[0].volume == 9999
    if defect is None:
        assert view.iloc[0].timestamp_evidence == 'four_hour_holiday_tail_ohl_matches_volume_differs'
        weekly = served_profile(daily.iloc[:1],daily,four,four.iloc[:0],cutoff,'1W',profile)
        assert weekly.index[0] == cutoff
        assert weekly.index[0] + pd.Timedelta(days=7) > cutoff


def test_service_profile_retains_fallback_bar_and_reports_it(tmp_path):
    from dataclasses import replace
    from test_analysis import settings
    from test_sync import FakeProvider
    from tv_history.storage import CsvStorage
    from tv_history.sync import HistorySynchronizer
    from tv_history.bars import AssetBarsService
    cfg = replace(settings(tmp_path), timestamp_profiles={'COMEX:GC1!':'cme_overnight'})
    storage = CsvStorage(cfg)
    storage.merge_and_write('COMEX:GC1!', frame(['2026-07-02T22:00Z','2026-07-06T22:00Z']), '1D')
    response = AssetBarsService(cfg, HistorySynchronizer(cfg, FakeProvider([]), storage)).get_bars(
        'COMEX:GC1!', '1D', '2026-07-06T00:00Z', count=3)
    assert 'error' not in response
    assert response['bars'][0]['t'] == '2026-07-03T00:00:00Z'
    assert response['timestamp_normalization']['unverified_rows'] == 1
