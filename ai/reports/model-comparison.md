# Model comparison

Macro-F1 on the held-out test split (20%).

| Model | Macro-F1 |
|---|---:|
| `random_forest` | 0.996 |
| `hist_gradient_boosting` | 0.996 |
| `logistic_regression` | 0.935 |

_HistGradientBoostingClassifier is the production model: best F1, fast inference, interpretable._