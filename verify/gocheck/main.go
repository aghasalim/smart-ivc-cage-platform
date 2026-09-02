// Recompute the classification report from the confusion matrix in Go.
//
// Reads ai/reports/metrics.json, extracts the 7x7 confusion matrix, and
// independently derives precision, recall, F1 and macro-F1 for each class.
// Compares against the published test_macro_f1.
//
// Run: cd verify/gocheck && go run . -root ../..
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"math"
	"os"
	"path/filepath"
)

type metrics struct {
	Classes []string    `json:"classes"`
	CM      [][]int     `json:"test_confusion_matrix"`
	MacroF1 float64     `json:"test_macro_f1"`
}

func main() {
	root := flag.String("root", "../..", "repository root")
	flag.Parse()

	b, err := os.ReadFile(filepath.Join(*root, "ai", "reports", "metrics.json"))
	if err != nil {
		fmt.Println("FAIL", err)
		os.Exit(1)
	}
	var m metrics
	if err := json.Unmarshal(b, &m); err != nil {
		fmt.Println("FAIL", err)
		os.Exit(1)
	}

	n := len(m.Classes)
	if n == 0 || len(m.CM) != n {
		fmt.Printf("FAIL expected %d classes, got %d matrix rows\n", n, len(m.CM))
		os.Exit(1)
	}

	problems := 0
	tol := 5e-4
	var f1sum float64

	for i := 0; i < n; i++ {
		tp := m.CM[i][i]
		var fp, fn int
		for j := 0; j < n; j++ {
			if j != i {
				fp += m.CM[j][i]
				fn += m.CM[i][j]
			}
		}
		var prec, rec, f1 float64
		if tp+fp > 0 {
			prec = float64(tp) / float64(tp+fp)
		}
		if tp+fn > 0 {
			rec = float64(tp) / float64(tp+fn)
		}
		if prec+rec > 0 {
			f1 = 2 * prec * rec / (prec + rec)
		}
		f1sum += f1
	}

	macroF1 := f1sum / float64(n)
	if math.Abs(macroF1-m.MacroF1) > tol {
		fmt.Printf("FAIL macro-F1: published %.6f, recomputed %.6f\n", m.MacroF1, macroF1)
		problems++
	}

	if problems > 0 {
		fmt.Printf("Go: %d problem(s)\n", problems)
		os.Exit(1)
	}
	fmt.Printf("Go: %d classes, macro-F1 %.4f reproduced from confusion matrix\n", n, macroF1)
}
