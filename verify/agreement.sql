-- Recompute the cumulative_agreement_pct column of the three online-learning
-- logs from the per-window agreement flags in the same files.
--
-- ai/reports/figures/online-agreement.png plots that column, and the final
-- value of it is the number the report quotes for how closely the streaming
-- SGD model tracks the fixed offline random forest. It was written by
-- ai/training/behavioral_monitoring/online_classifier.py, which accumulates it
-- incrementally while the model trains, so a bug in the accumulator would be
-- invisible: the plot reads the same column that produced it.
--
-- The definition being reproduced (online_classifier.py:203-207) is a running
-- mean of the agreement flag over windows from MIN_WINDOWS_BEFORE_EVAL = 10
-- onwards, zero during the warm-up, published rounded to two decimals.
--
-- Run: sqlite3 :memory: -init verify/agreement.sql "" < /dev/null

.mode list
.headers off
.import --csv ai/data/behavioral_monitoring/outputs/sgd_learning_log.csv           a
.import --csv ai/data/behavioral_monitoring/outputs/sgd_v2_learning_log.csv        b
.import --csv ai/data/behavioral_monitoring/outputs/sgd_threshold_learning_log.csv c

CREATE TEMP VIEW logs AS
    SELECT 'sgd'       AS src, CAST("window" AS INTEGER) AS w, agreement AS ag,
           CAST(cumulative_agreement_pct AS REAL) AS pub FROM a
    UNION ALL
    SELECT 'sgd_v2',        CAST("window" AS INTEGER), agreement,
           CAST(cumulative_agreement_pct AS REAL) FROM b
    UNION ALL
    SELECT 'sgd_threshold', CAST("window" AS INTEGER), agreement,
           CAST(cumulative_agreement_pct AS REAL) FROM c;

CREATE TEMP VIEW running AS
    SELECT src, w, pub,
           SUM(CASE WHEN w >= 10 AND ag = 'True' THEN 1 ELSE 0 END)
               OVER (PARTITION BY src ORDER BY w
                     ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS hits
    FROM logs;

CREATE TEMP VIEW recomputed AS
    SELECT src, w, pub,
           CASE WHEN w >= 10 THEN 100.0 * hits / (w - 10 + 1) ELSE 0.0 END AS got
    FROM running;

-- The published column is rounded to two decimals, so anything at or under
-- half a unit in the last place is that rounding and not a disagreement.
SELECT 'MISMATCH ' || src || ' window=' || w || ' got=' || got || ' published=' || pub
    FROM recomputed WHERE abs(got - pub) > 0.0051;

SELECT CASE
    WHEN (SELECT COUNT(*) FROM recomputed) <> 3039
        THEN 'SQL FAIL: expected 3039 rows, read ' || (SELECT COUNT(*) FROM recomputed)
    WHEN (SELECT COUNT(*) FROM recomputed WHERE abs(got - pub) > 0.0051) > 0
        THEN 'SQL FAIL: ' || (SELECT COUNT(*) FROM recomputed WHERE abs(got - pub) > 0.0051)
             || ' of 3039 rows disagree'
    ELSE 'SQL OK 3039 rows reproduced, max abs diff '
         || printf('%.3e', (SELECT MAX(abs(got - pub)) FROM recomputed))
         || ', final agreement '
         || (SELECT group_concat(src || '=' || printf('%.2f', pub), ' ')
             FROM recomputed r WHERE w = (SELECT MAX(w) FROM recomputed x WHERE x.src = r.src))
END;
