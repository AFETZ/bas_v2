# Передача полных артефактов

Компактные таблицы, рисунки и рукопись входят в Git. Полные raw56 занимают
**57 325 606 769 байт,10 798 файлов**; [инвентарь с SHA256 каждого файла](../data/raw-inventory/raw_files.csv)
и [суммы по запускам](../data/raw-inventory/raw_runs.csv). Хэши вычислены после
остановки runtime: ошибок/изменений во время чтения не обнаружено. Это инвентарь
передачи, не доказательство научного тезиса вместо анализа содержимого.

Полный архив здесь не создавался и никуда не загружался. Для передачи с данного
подготовленного хоста можно выполнить из корня репозитория:

```bash
python3 - <<'PY'
import json
from pathlib import Path
f = json.loads(Path('runs/urban-ns3-20261028/freeze.json').read_text())
paths = ['runs/native-radio-realtime/' + r['run_id'] for r in f['cases']]
paths += ['runs/urban-ns3-20261028']
Path('/tmp/bas-urban-archive-inputs.txt').write_text('\n'.join(paths) + '\n')
PY
flock -n -E 75 /tmp/bas-paper-runtime.lock tar -cf \
 /home/bas/BAS_ISPRAS_2026/urban-raw-20261029.tar \
 -T /tmp/bas-urban-archive-inputs.txt
```

Целевой архив расположен вне перечисленных исходных каталогов. В study-каталог
входят исходный/окончательный анализ, контроллеры, команды, freeze, seeds,
все failed attempts и инвентарь. Старые main30/sensitivity18 не добавлены к новой
когорте. Пилоты при необходимости передаются отдельно с их подлинными ID.

Закреплённый runtime image не распространяется одним только SHA256. Для его
сохранения на этом хосте, отдельно от raw:

```bash
flock -n -E 75 /tmp/bas-paper-runtime.lock docker image save \
 sha256:89d78eff9914b1644a1b01b793612e9ee8b19916c5a7bf5f578ba6d8bfbfafe5 \
 -o /home/bas/BAS_ISPRAS_2026/urban-runtime-image.tar
```

Для нового хоста остаются нужны pinned native fork/build, Iris/ROS/Gazebo
plugins и полный build manifest; stock build без ранее установленных Sionna
пакетов не проверен. Новая сцена корпусов генерируется из JSON. Настоящий новый
эксперимент использует свежие ID; восстановление архивов не является новым опытом.
