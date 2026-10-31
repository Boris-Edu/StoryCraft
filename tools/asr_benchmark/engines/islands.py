"""
Sound-island clustering, copied from BadWords' chunked faster-whisper runner
(src/engine/transcription.py, run_whisper() template: RAW_ISLANDS -> ISLANDS).

BadWords transcribes each cluster separately with the verbatim initial prompt. Whisper only
applies an initial prompt to the first 30 s window of a transcribe() call, so per-cluster calls
are what keep the prompt (and with it, fillers) active across the whole recording. Engines that
should be compared like-for-like with BadWords use the same clusters.

Keep in sync with the runner if its constants or rules change.
"""
MAX_CLUSTER_DUR = 22.0
MIN_CLUSTER_DUR = 8.0
MIN_SAFE_GAP = 0.5


def cluster_islands(raw):
    clusters = []
    i = 0
    while i < len(raw):
        c_start = raw[i][0]
        J = []
        for j in range(i, len(raw)):
            if raw[j][1] - c_start <= MAX_CLUSTER_DUR:
                J.append(j)
            else:
                break
        if not J:
            J = [i]

        if J[-1] == len(raw) - 1:
            best_j = J[-1]
        else:
            optimal, safe = [], []
            for j in J:
                gap = raw[j + 1][0] - raw[j][1]
                dur = raw[j][1] - c_start
                if gap >= MIN_SAFE_GAP:
                    safe.append(j)
                    if dur >= MIN_CLUSTER_DUR:
                        optimal.append(j)
            pool = optimal or safe or J
            best_j = max(pool, key=lambda j: raw[j + 1][0] - raw[j][1])

        clusters.append((c_start, raw[best_j][1]))
        i = best_j + 1
    return clusters
