/*
 * Recompute the published classification report from the confusion matrix, in C.
 *
 * ai/reports/metrics.json is a single scikit-learn dump: the 7x7 test confusion
 * matrix, a per-class precision/recall/F1/support report, and the macro-F1 the
 * README quotes as 0.996. The report and the headline are functions of the
 * matrix, but nothing in the repository ever checked that they agree with it,
 * because everything downstream reads the report rather than the matrix.
 *
 * This reads the same JSON with a hand-written scanner, resolves every class BY
 * NAME rather than by position (a transposed or reordered matrix would then show
 * up as a precision/recall swap), and recomputes all of it.
 *
 * Build: cc -std=c99 -O2 -Wall -Wextra -Wpedantic -Werror -o report verify/report.c -lm
 * Run:   ./report <repo root>
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>

#define MAXC 16
#define TOL 1e-12

static char *slurp(const char *path) {
    FILE *f = fopen(path, "rb");
    if (!f) { fprintf(stderr, "cannot open %s\n", path); return NULL; }
    if (fseek(f, 0, SEEK_END) != 0) { fclose(f); return NULL; }
    long n = ftell(f);
    if (n < 0) { fclose(f); return NULL; }
    rewind(f);
    char *buf = malloc((size_t)n + 1);
    if (!buf) { fclose(f); return NULL; }
    if (fread(buf, 1, (size_t)n, f) != (size_t)n) { free(buf); fclose(f); return NULL; }
    buf[n] = '\0';
    fclose(f);
    return buf;
}

/* Position just past the given quoted key, or NULL. */
static const char *key(const char *hay, const char *name) {
    char pat[128];
    snprintf(pat, sizeof pat, "\"%s\"", name);
    const char *p = strstr(hay, pat);
    return p ? p + strlen(pat) : NULL;
}

/* Value of "name": <number> searching forward from hay. */
static int num(const char *hay, const char *name, double *out) {
    const char *p = key(hay, name);
    if (!p) return 0;
    while (*p && *p != ':') p++;
    if (!*p) return 0;
    *out = strtod(p + 1, NULL);
    return 1;
}

static int failures = 0;
static double maxerr = 0.0;
static int compared = 0;

static void cmp(const char *what, double got, double want) {
    double e = fabs(got - want);
    compared++;
    if (e > maxerr) maxerr = e;
    if (!(e <= TOL)) {
        printf("MISMATCH %-24s got=%.17g want=%.17g diff=%.3e\n", what, got, want, e);
        failures++;
    }
}

int main(int argc, char **argv) {
    const char *root = argc > 1 ? argv[1] : ".";
    char path[4096];
    snprintf(path, sizeof path, "%s/ai/reports/metrics.json", root);
    char *js = slurp(path);
    if (!js) return 2;

    /* Class names, in matrix order. */
    char names[MAXC][64];
    int nc = 0;
    const char *p = key(js, "classes");
    if (!p) { fprintf(stderr, "no classes key\n"); return 2; }
    while (*p && *p != ']') {
        if (*p == '"') {
            const char *e = strchr(p + 1, '"');
            if (!e) break;
            size_t len = (size_t)(e - p - 1);
            if (nc >= MAXC || len >= sizeof names[0]) { fprintf(stderr, "too many classes\n"); return 2; }
            memcpy(names[nc], p + 1, len);
            names[nc][len] = '\0';
            nc++;
            p = e + 1;
        } else p++;
    }
    if (nc < 2) { fprintf(stderr, "parsed %d classes\n", nc); return 2; }

    /* Confusion matrix: nc*nc integers between the outer brackets. */
    long cm[MAXC][MAXC];
    p = key(js, "test_confusion_matrix");
    if (!p) { fprintf(stderr, "no confusion matrix\n"); return 2; }
    int got = 0;
    while (*p && got < nc * nc) {
        if ((*p >= '0' && *p <= '9') || *p == '-') {
            char *end;
            long v = strtol(p, &end, 10);
            cm[got / nc][got % nc] = v;
            got++;
            p = end;
        } else if (*p == ']' && got == nc * nc) {
            break;
        } else p++;
    }
    if (got != nc * nc) { fprintf(stderr, "read %d of %d matrix cells\n", got, nc * nc); return 2; }

    const char *rep = strstr(js, "\"test_classification_report\"");
    if (!rep) { fprintf(stderr, "no report\n"); return 2; }

    long total = 0, correct = 0;
    double f1sum = 0.0, wsum = 0.0;
    for (int i = 0; i < nc; i++) {
        long tp = cm[i][i], fp = 0, fn = 0, sup = 0;
        for (int j = 0; j < nc; j++) {
            sup += cm[i][j];
            if (j != i) { fn += cm[i][j]; fp += cm[j][i]; }
        }
        total += sup;
        correct += tp;

        double prec = (double)tp / (double)(tp + fp);
        double rec  = (double)tp / (double)(tp + fn);
        double f1   = 2.0 * (double)tp / (double)(2 * tp + fp + fn);
        f1sum += f1;
        wsum  += f1 * (double)sup;

        /* Resolve the published block by class name, not by index. */
        char pat[128];
        snprintf(pat, sizeof pat, "\"%s\"", names[i]);
        const char *blk = strstr(rep, pat);
        if (!blk) { fprintf(stderr, "class %s missing from report\n", names[i]); return 2; }
        const char *endblk = strchr(blk, '}');
        if (!endblk) { fprintf(stderr, "unterminated block for %s\n", names[i]); return 2; }
        size_t blen = (size_t)(endblk - blk);
        char *block = malloc(blen + 1);
        if (!block) return 2;
        memcpy(block, blk, blen);
        block[blen] = '\0';

        double wp, wr, wf, ws;
        if (!num(block, "precision", &wp) || !num(block, "recall", &wr) ||
            !num(block, "f1-score", &wf) || !num(block, "support", &ws)) {
            fprintf(stderr, "incomplete block for %s\n", names[i]);
            free(block);
            return 2;
        }
        free(block);

        char what[128];
        snprintf(what, sizeof what, "%s.precision", names[i]); cmp(what, prec, wp);
        snprintf(what, sizeof what, "%s.recall",    names[i]); cmp(what, rec,  wr);
        snprintf(what, sizeof what, "%s.f1",        names[i]); cmp(what, f1,   wf);
        snprintf(what, sizeof what, "%s.support",   names[i]); cmp(what, (double)sup, ws);
    }

    const char *macro = strstr(rep, "\"macro avg\"");
    const char *weight = strstr(rep, "\"weighted avg\"");
    double v;
    if (!macro || !weight) { fprintf(stderr, "no averages\n"); return 2; }
    if (num(macro, "f1-score", &v))  cmp("macro_avg.f1", f1sum / nc, v);
    if (num(weight, "f1-score", &v)) cmp("weighted_avg.f1", wsum / (double)total, v);
    if (num(rep, "accuracy", &v))    cmp("accuracy", (double)correct / (double)total, v);
    if (num(js, "test_macro_f1", &v)) cmp("test_macro_f1", f1sum / nc, v);
    if (num(js, "n_test", &v))       cmp("n_test", (double)total, v);

    free(js);

    if (compared != nc * 4 + 5) {
        printf("C FAIL: made %d comparisons, expected %d\n", compared, nc * 4 + 5);
        return 1;
    }
    if (failures) {
        printf("C FAIL: %d of %d values disagree\n", failures, compared);
        return 1;
    }
    printf("C OK %d values reproduced from the %dx%d confusion matrix, max abs diff %.3e\n",
           compared, nc, nc, maxerr);
    return 0;
}
