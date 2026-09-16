"""Readable persisted metadata; runtime views retain the existing API shape."""
from copy import deepcopy
import pandas as pd

DAYS = ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday')

def clock(minutes):
    return f'{minutes // 60:02d}:{minutes % 60:02d}'

def minutes(value):
    h, m = map(int, value.split(':'))
    return h * 60 + m

def compact_calendar(calendar):
    if not calendar or calendar.get('schema_version') == 2:
        return deepcopy(calendar)
    out = {k: deepcopy(v) for k, v in calendar.items()
           if k not in ('rules', 'session_templates', 'rejected_sessions', 'requested_sessions', 'schema_version')}
    out['schema_version'] = 2
    sessions = {}
    for day, templates in calendar.get('session_templates', {}).items():
        spans = []
        for template in templates:
            points = sorted(d * 1440 + h * 60 + m for d, h, m in template)
            for point in points:
                if spans and spans[-1][1] == point:
                    spans[-1][1] += 60
                else:
                    spans.append([point, point + 60])
        sessions[DAYS[int(day)]] = [
            {'start': clock(a % 1440), 'end': clock(b % 1440),
             **({'start_day_offset': a // 1440} if a // 1440 else {}),
             **({'end_day_offset': b // 1440} if b // 1440 else {})}
            for a, b in spans]
    out['sessions'] = sessions
    # A verified native bar points into the ONE shared session calendar.
    # Only provider ownership that does not fit a session uses an explicit close.
    alignments = {}
    for key, rule in calendar.get('rules', {}).items():
        tf, opening = key.split('|'); day, time = opening.split(':', 1); day = int(day)
        d, h, m = rule['offset']; target = (day + d) % 7
        match = None
        for weekday, spans in sessions.items():
            for i, span in enumerate(spans):
                if ((DAYS.index(weekday) + span.get('end_day_offset', 0)) % 7 == target
                        and span['end'] == clock(h * 60 + m)):
                    match = {'session': f'{weekday}/{i + 1}', 'close_day_offset': d}
                    break
            if match: break
        entry = match or {'close': clock(h * 60 + m), 'close_day_offset': d}
        # Exact resumption remains necessary to distinguish missing data from breaks.
        nd, nh, nm = rule['next_offset']
        candidates = []
        absolute_close = (day + d) * 1440 + h * 60 + m
        for weekday, spans in sessions.items():
            for span in spans:
                for week in range(-1, 4):
                    point = (DAYS.index(weekday) + span.get('start_day_offset', 0) + week * 7) * 1440 + minutes(span['start'])
                    if point >= absolute_close: candidates.append(point)
        predicted = min(candidates) if candidates else None
        actual = (day + nd) * 1440 + nh * 60 + nm
        if predicted != actual:
            entry['next_open_override'] = {'day_offset': nd, 'time': clock(nh * 60 + nm)}
        alignments.setdefault(tf, {}).setdefault(DAYS[day], {})[time] = entry
    out['bar_alignment'] = alignments
    return out

def expand_calendar(document):
    if not document or document.get('schema_version') != 2:
        return deepcopy(document)
    out = {k: deepcopy(v) for k, v in document.items() if k not in ('sessions', 'bar_alignment')}
    out['schema_version'] = 1
    sessions = document.get('sessions', {})
    out['session_templates'] = {}
    for weekday, spans in sessions.items():
        patterns = []
        for span in spans:
            start = span.get('start_day_offset', 0) * 1440 + minutes(span['start'])
            end = span.get('end_day_offset', 0) * 1440 + minutes(span['end'])
            patterns.append([[p // 1440, p % 1440 // 60, p % 60] for p in range(start, end, 60)])
        out['session_templates'][str(DAYS.index(weekday))] = patterns
    out['rules'] = {}
    for tf, weekdays in document.get('bar_alignment', {}).items():
        for weekday, entries in weekdays.items():
            day = DAYS.index(weekday)
            for opening, entry in entries.items():
                d = entry['close_day_offset']
                if 'session' in entry:
                    name, number = entry['session'].split('/')
                    close = sessions[name][int(number) - 1]['end']
                else: close = entry['close']
                end = (day + d) * 1440 + minutes(close)
                override = entry.get('next_open_override')
                if override:
                    nxt = (day + override['day_offset']) * 1440 + minutes(override['time'])
                else:
                    candidates = []
                    for name, spans in sessions.items():
                        for span in spans:
                            for week in range(-1, 4):
                                point = (DAYS.index(name) + span.get('start_day_offset', 0) + week * 7) * 1440 + minutes(span['start'])
                                if point >= end: candidates.append(point)
                    nxt = min(candidates)
                out['rules'][f'{tf}|{day}:{opening}'] = {
                    'offset': [d, minutes(close) // 60, minutes(close) % 60],
                    'next_offset': [nxt // 1440 - day, nxt % 1440 // 60, nxt % 60]}
    return out

def gap_key(gap):
    return f"tail|{gap['after']}" if gap.get('terminal') else f"{gap['after']}|{gap['before']}"

def compact_coverage(value):
    out = deepcopy(value)
    out.get('startup_check', {}).pop('result', None)
    for tf in ('1h', '4h', '1D', '1W'):
        state = out.get(tf, {})
        attempted = set(state.pop('attempted_gaps', []))
        for gap in state.get('unresolved_gaps', []):
            if gap_key(gap) in attempted: gap['repair_attempted'] = True
    return out

def expand_coverage(value):
    out = deepcopy(value)
    for tf in ('1h', '4h', '1D', '1W'):
        state = out.get(tf, {})
        attempted = set(state.get('attempted_gaps', []))
        for gap in state.get('unresolved_gaps', []):
            if gap.pop('repair_attempted', False): attempted.add(gap_key(gap))
        if state: state['attempted_gaps'] = sorted(attempted)
    return out

def prune_receipts(receipts, index, calendar):
    """Drop evidence only when no cutoff can use it before a successor.

    The calendar cannot authorize boundaries before effective_from. If a bar's
    successor already exists by that date, every earlier cutoff is gated and
    every later cutoff uses the successor. Keep all other evidence conservatively.
    """
    if not calendar or not calendar.get('effective_from') or len(index) < 2:
        return receipts
    effective = pd.Timestamp(calendar['effective_from'])
    removable = {stamp.isoformat() for stamp, successor in zip(index[:-1], index[1:])
                 if successor <= effective}
    return {k: {field: val for field, val in v.items() if field != 'received_at'}
            for k, v in receipts.items() if k not in removable}
