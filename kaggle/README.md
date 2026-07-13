# Kaggle — הזרוע הענן של הלינקר

‏session חד-פעמי של Kaggle‏ (GPU + אינטרנט) נרשם כ-GitHub JIT runner‏ (job אחד בדיוק),
מריץ את relink.yml הרגיל במלואו — אותו stack, אותם שערים (fingerprint, ‏linkerStrict,
‏publish→baseline) — ונמחק כליל בסיומו. אין מה לנקות ואין מצב שנשאר.

## שיגור

- מה-repo: ‏Actions → ‏kaggle-relink → ‏Run workflow (אופציונלי dry_run).
- מקומית: ‏`scripts/dispatch_kaggle_relink.sh [--dry-run]`‏ (דורש gh עם admin + kaggle CLI מחובר).

הסדר חשוב וכבר ממומש בסקריפט: קודם נרשם ה-JIT ונכנס ה-job לתור (target=kaggle),
ואז נדחף הקרנל — ה-session עולה ישר לתוך העבודה הממתינה.

## מתי קגל ומתי השרת

| תרחיש | יעד |
|---|---|
| ‏relink סריאלי בתוך בנייה שבועית (דלתא קטנה, הבנייה ממתינה) | שרת — זמינות דטרמיניסטית |
| ‏relink עצמאי / עומס כבד / הרחקת עבודה מהשרת | קגל |
| ‏relink מלא של כל הקורפוס | אף אחד מהם ישירות — ראו למטה |

## עובדות שנמדדו (07/2026)

- העומס הוא **resolution-bound, לא NER-bound**: ‏GPU T4 נתן ‏7.2M מילים/שעה מול ‏6.4M
  על ה-ARM — ההאצה זניחה כי צוואר-הבקבוק הוא פייתון/CPU‏ (4 ליבות בשני המקומות).
- ‏relink מלא = ‏~421M מילים ≈ ‏58 שעות ≈ פי-2 ממכסת ה-GPU השבועית (30h) — לא ריאלי
  ב-session בודד (תקרה ~9h). לשינויי-מנוע שהם output-neutral יש מסלול מוסדר:
  ‏`--adopt-fingerprint OLD::NEW`‏ (אטסטציית מפעיל, ראו incremental.py). שינוי מנוע
  שמחייב באמת relink מלא ידרוש פריסה על sessions‏ (--max-books, טרם ממומש) או את השרת.

## מלכודות סביבת Kaggle (כולן מטופלות ב-bootstrap.sh)

- אף פייתון בתמונה לא יוצר venv תקין → מותקן python3.12-venv.
- ‏PYTHONPATH גלובלי מזהם venvs נקיים → unset.
- ‏sitecustomize.py שתול בפייתון-המערכת מיירט `google.cloud` ודורש kaggle_gcp → נמחק.
- אין mongod/mongosh/mongorestore/gh → מותקנים מ-tarballs נעוצי-גרסה.
- ‏runner כ-root → ‏RUNNER_ALLOW_RUNASROOT=1.
