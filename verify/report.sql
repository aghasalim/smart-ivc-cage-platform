-- Recompute the published classification report from the confusion matrix.
--
-- ai/reports/metrics.json publishes three things that are not independent:
-- the 7x7 test confusion matrix, a per-class precision/recall/F1/support
-- report, and the headline macro-F1 that the README quotes as 0.996. All three
-- were written by one scikit-learn call, so nothing ever checked that the
-- report and the headline actually follow from the matrix. This derives them
-- again in SQL, reading the same JSON file and using nothing from Python.
--
-- Run: sqlite3 :memory: -init verify/report.sql "" < /dev/null
--      (from the repository root)

.mode list
.headers off

CREATE TEMP TABLE j AS
    SELECT CAST(readfile('ai/reports/metrics.json') AS TEXT) AS t;

-- Confusion matrix as (true class index, predicted class index, count).
CREATE TEMP TABLE cm AS
    SELECT r.key AS i, c.key AS p, CAST(c.value AS INTEGER) AS n
    FROM j,
         json_each(json_extract(j.t, '$.test_confusion_matrix')) AS r,
         json_each(r.value) AS c;

CREATE TEMP TABLE cls AS
    SELECT key AS i, value AS name
    FROM j, json_each(json_extract(j.t, '$.classes'));

-- Columns are resolved by class name, not by position: the report is a JSON
-- object keyed by name and the matrix is keyed by index, and this is the join
-- that would catch a row/column transposition.
CREATE TEMP VIEW counts AS
    SELECT cls.name AS name,
           (SELECT n FROM cm WHERE cm.i = cls.i AND cm.p = cls.i)          AS tp,
           (SELECT SUM(n) FROM cm WHERE cm.p = cls.i AND cm.i <> cls.i)    AS fp,
           (SELECT SUM(n) FROM cm WHERE cm.i = cls.i AND cm.p <> cls.i)    AS fn,
           (SELECT SUM(n) FROM cm WHERE cm.i = cls.i)                      AS support
    FROM cls;

CREATE TEMP VIEW derived AS
    SELECT name, support,
           CAST(tp AS REAL) / (tp + fp) AS prec,
           CAST(tp AS REAL) / (tp + fn) AS rec,
           2.0 * tp / (2.0 * tp + fp + fn) AS f1
    FROM counts;

CREATE TEMP VIEW published AS
    SELECT c.name AS name,
           json_extract(j.t, '$.test_classification_report."' || c.name || '".precision') AS prec,
           json_extract(j.t, '$.test_classification_report."' || c.name || '".recall')    AS rec,
           json_extract(j.t, '$.test_classification_report."' || c.name || '"."f1-score"') AS f1,
           json_extract(j.t, '$.test_classification_report."' || c.name || '".support')   AS support
    FROM cls c, j;

-- Per-class cells, then the four aggregates the README and the report quote.
CREATE TEMP VIEW cells AS
    SELECT d.name || '.precision' AS cell, d.prec AS got, p.prec AS want
        FROM derived d JOIN published p USING (name)
    UNION ALL
    SELECT d.name || '.recall', d.rec, p.rec FROM derived d JOIN published p USING (name)
    UNION ALL
    SELECT d.name || '.f1', d.f1, p.f1 FROM derived d JOIN published p USING (name)
    UNION ALL
    SELECT d.name || '.support', d.support, p.support FROM derived d JOIN published p USING (name)
    UNION ALL
    SELECT 'macro_avg.f1', (SELECT AVG(f1) FROM derived),
           (SELECT json_extract(t, '$.test_classification_report."macro avg"."f1-score"') FROM j)
    UNION ALL
    SELECT 'test_macro_f1', (SELECT AVG(f1) FROM derived),
           (SELECT json_extract(t, '$.test_macro_f1') FROM j)
    UNION ALL
    SELECT 'weighted_avg.f1',
           (SELECT SUM(f1 * support) / SUM(support) FROM derived),
           (SELECT json_extract(t, '$.test_classification_report."weighted avg"."f1-score"') FROM j)
    UNION ALL
    SELECT 'accuracy',
           (SELECT CAST(SUM(n) AS REAL) FROM cm WHERE i = p) / (SELECT SUM(n) FROM cm),
           (SELECT json_extract(t, '$.test_classification_report.accuracy') FROM j)
    UNION ALL
    SELECT 'n_test', (SELECT SUM(n) FROM cm), (SELECT json_extract(t, '$.n_test') FROM j);

SELECT 'MISMATCH ' || cell || ' got=' || got || ' want=' || want
    FROM cells WHERE abs(got - want) > 1e-12;

SELECT CASE
    WHEN (SELECT COUNT(*) FROM cells) <> 33
        THEN 'SQL FAIL: expected 33 comparisons, made ' || (SELECT COUNT(*) FROM cells)
    WHEN (SELECT COUNT(*) FROM cells WHERE abs(got - want) > 1e-12) > 0
        THEN 'SQL FAIL: ' || (SELECT COUNT(*) FROM cells WHERE abs(got - want) > 1e-12)
             || ' of 33 values disagree'
    ELSE 'SQL OK 33 values reproduced, max abs diff '
         || printf('%.3e', (SELECT MAX(abs(got - want)) FROM cells))
END;
