# Исходные входы замороженного launcher

Это байтовые копии существовавших файлов, не реконструкция журналов.
`archive_sources.json` указывает исходные пути, размеры и SHA256.

- `freeze.json`: новая серия56 на4d96d19, а не результаты56.
- `plan.json`: порядок основной и вспомогательных когорт.
- `base_freeze_20261026.json`: действительный старый base environment; исходный
  main30 не перезаписывается. Этот вход нужен текущему launcher даже с `--freeze`.

На новом clone из корня репозитория восстановить отсутствующий вход:

```bash
python3 - <<'PY'
from pathlib import Path
src = Path('reports/ispras/urban_ns3/reproduction/base_freeze_20261026.json')
dst = Path('runs/paper-live-20261026/campaign/freeze.json')
if dst.exists():
    assert dst.read_bytes() == src.read_bytes(), 'Existing historical freeze preserved'
else:
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(src.read_bytes())
PY
```

Сама эта операция не предоставляет runtime image, внешнюю сборку либо full raw.
Новый полноценный повтор требует нового study ID; старые ID не используются повторно.
