import pandas as pd
from tv_history.metadata import compact_calendar, expand_calendar, compact_coverage, expand_coverage, prune_receipts
from tv_history.calendar import expected_boundary, suspend_conflicts
from tv_history.finality import completion_details
from test_calendar_finality import calendar, with_evidence, evaluate


def test_shared_calendar_preserves_all_verified_boundaries(tmp_path):
    old = calendar(tmp_path)
    doc = compact_calendar(old)
    assert 'rules' not in doc and 'session_templates' not in doc
    assert 'Monday' in doc['sessions']
    new = expand_calendar(doc)
    assert set(new['rules']) == set(old['rules'])
    for key, rule in old['rules'].items():
        assert new['rules'][key]['offset'] == list(rule['offset'])
        assert new['rules'][key]['next_offset'] == list(rule['next_offset'])
    assert compact_calendar(new) == doc
    for tf, stamp in [('1h','2026-09-01T15:00Z'),('4h','2026-09-01T13:00Z'),('1D','2026-09-01T09:00Z'),('1W','2026-09-07T09:00Z')]:
        opened = pd.Timestamp(stamp)
        for now in [opened, opened + pd.Timedelta(days=8)]:
            assert expected_boundary(old,tf,opened,now) == expected_boundary(new,tf,opened,now)


def test_shared_calendar_overnight_and_provider_override():
    old = {'schema_version':1, 'session_templates':{'0': [[[0,22,0],[0,23,0],[1,0,0]]], '1': [[[0,22,0]]]},
           'rules':{'4h|0:22:00':{'offset':[1,1,0],'next_offset':[1,22,0]},
                    '1W|0:22:00':{'offset':[8,1,0],'next_offset':[9,22,0]}}}
    doc=compact_calendar(old); new=expand_calendar(doc)
    assert doc['sessions']['Monday'][0]['end_day_offset']==1
    assert 'next_open_override' in doc['bar_alignment']['1W']['Monday']['22:00']
    assert new['rules']==old['rules']


def test_gap_state_has_one_copy_and_discards_resolved_attempts():
    gap={'after':'a','before':'b','status':'uncertain'}
    old={'startup_check':{'checked_at':'today','result':{'1h':[gap]}},
         '1h':{'unresolved_gaps':[gap], 'attempted_gaps':['a|b','old|resolved']}}
    doc=compact_coverage(old)
    assert 'result' not in doc['startup_check']
    assert 'attempted_gaps' not in doc['1h']
    assert doc['1h']['unresolved_gaps'][0]['repair_attempted']
    assert expand_coverage(doc)['1h']['attempted_gaps']==['a|b']
    assert 'repair_attempted' not in gap


def test_receipt_pruning_preserves_historical_cutoffs_and_tip(tmp_path):
    cal=calendar(tmp_path)
    stamps=pd.DatetimeIndex(['2026-08-31T15:00Z','2026-09-01T09:00Z','2026-09-01T15:00Z','2026-09-02T09:00Z'])
    receipts={s.isoformat():{'request_started_at':'2026-09-02T20:00:00Z','received_at':'2026-09-02T20:00:01Z','ohlcv':[100.,101.,99.,100.,100.]} for s in stamps}
    # Add a definitely redundant older record with a pre-effective successor.
    stamps=stamps.insert(0,pd.Timestamp('2026-08-28T15:00Z'))
    receipts[stamps[0].isoformat()]=dict(receipts[stamps[1].isoformat()])
    reduced=prune_receipts(receipts,stamps,cal)
    assert stamps[0].isoformat() not in reduced
    assert stamps[-1].isoformat() in reduced
    assert stamps[2].isoformat() in reduced
    for cutoff in ['2026-08-28T16:05Z','2026-09-01T00:00Z','2026-09-01T16:05Z','2026-09-02T09:00Z']:
        for stamp in stamps[stamps<=pd.Timestamp(cutoff)]:
            f=with_evidence(cal,'1h',stamp)
            ctx=f.attrs['completion_context'];ctx['raw_opens']=list(stamps);ctx['receipts']=receipts
            before=evaluate(f,'1h',cutoff)
            ctx['receipts']=reduced
            assert before.equals(evaluate(f,'1h',cutoff))
