# Анонимный черновик статьи

Основной источник — `src/manuscript.md`. Официальный исходный шаблон и его хеш — `template/`. Проверенные первичные ссылки, карта утверждений и требования на 27.10.2026 — `refs/`. Числа относятся к 30 замороженным запускам реализации `8e16ec2`; новая серия чувствительности и новый операторский опыт пока явно обозначены как отдельная незавершённая работа. `check_manuscript.py --final` отклоняет такие placeholders. Для будущего фактического 18-строчного экспорта подготовлены renderer `data/sensitivity_runs.csv` и таблица `@table sensitivity`; до появления полного экспорта они не выдают график либо результаты.

Готовые файлы для чтения: `build/manuscript_anonymous.docx` и `build/manuscript_anonymous.pdf`. Это научный черновик, не отправленная статья. Для внешнего рецензирования предназначены эти два файла после проверки; **не исходный DOTM и не весь каталог evidence**, содержащие сведения примера или внутренние пути. Текст не содержит придуманных авторов, DOI, тома, номера и страниц публикации.

## Сборка

Отдельный образ с документными инструментами не меняет runtime image симуляторов. Все рендеры выполняются вне измерительного окна, под общим runtime lock. Сборка образа из интеграционной копии:

```bash
flock --nonblock --conflict-exit-code 75 /tmp/bas-paper-runtime.lock \
  docker build -t bas-paper-doc-tools:20261027 -f paper/tools/Dockerfile paper/tools
```

Код 75 означает занятый lock: не удалять его и не останавливать чужой runtime. Команды ниже выполняются из корня рабочей копии с доступным Docker. Используется UID/GID текущего пользователя; системные переменные не переопределяются.

```bash
flock --nonblock --conflict-exit-code 75 /tmp/bas-paper-runtime.lock \
  bash paper/tools/render_document.sh
```

Этот короткий batch последовательно строит рисунки, DOCX и PDF. Отдельные команды, если требуется только один этап:

```bash
flock --nonblock --conflict-exit-code 75 /tmp/bas-paper-runtime.lock \
  docker run --rm --user "$(id -u):$(id -g)" \
  -e MPLCONFIGDIR=/tmp/bas-paper-mpl \
  -v "$PWD/paper:/work/paper" bas-paper-doc-tools:20261027 \
  python3 /work/paper/tools/make_figures.py

docker run --rm --user "$(id -u):$(id -g)" \
  -v "$PWD/paper:/work/paper" bas-paper-doc-tools:20261027 \
  python3 /work/paper/tools/build_docx.py

flock --nonblock --conflict-exit-code 75 /tmp/bas-paper-runtime.lock \
  docker run --rm --user "$(id -u):$(id -g)" --cpus 1 \
  -v "$PWD/paper:/work/paper" bas-paper-doc-tools:20261027 \
  libreoffice -env:UserInstallation=file:///tmp/bas-paper-lo \
  --headless --convert-to pdf --outdir /work/paper/build \
  /work/paper/build/manuscript_anonymous.docx

pdfinfo paper/build/manuscript_anonymous.pdf
pdffonts paper/build/manuscript_anonymous.pdf
pdftotext -layout paper/build/manuscript_anonymous.pdf paper/build/manuscript_anonymous.txt
python3 paper/tools/check_manuscript.py
```

В DOCX сохранены официальные названия Times New Roman/Arial, размеры 10/16/11 пт и стили шаблона. В текущем Linux-образе отсутствуют лицензионные Microsoft-шрифты; PDF встраивает Liberation Serif/Sans как метрически совместимые замены. Это ограничение чернового PDF, а не скрытое изменение требований. Финальная пагинация после открытия с оригинальными шрифтами может отличаться. Установка лицензируемых шрифтов автоматически не выполнялась.

## Данные рисунков

`data/six_cells.csv`, `selective_paired_points.csv`, `selective_paired_estimates.csv` скопированы из завершённых анализов основной серии, `decomposition.csv` — из отдельного raw-пересчёта. В бумажных копиях CSV нормализованы только окончания строк до LF для переноса через Git; числовые записи не менялись. `data/provenance.json` фиксирует источники относительно evidence bundle, SHA256 исходных байтов и нормализованных копий. Исходные raw не изменялись. `first_pair.json.gz` содержит данные первой заранее выбранной пары с исходными пропусками. Текст/оси используют относительное host-monotonic время; личные пути и имена отсутствуют.

Повторное извлечение первой пары **читает** существующий research loader из сохранённого evidence bundle; симуляторы не запускаются:

```bash
python3 paper/tools/extract_first_pair.py \
  --evidence-root runs/paper-live-20261026 \
  --raw-root runs/native-radio-realtime \
  --plan network/config/relay_campaign_30.csv
```

Первое planned pair определяется строками 1–2 плана (RngRun=4), а не качеством результата. Новый PNG/SVG/PDF рендерится из тех же поз и реально допущенных целей; состояние target не интерполируется. Пять парных точек и интервалы остаются доступны в CSV. У decomposition отдельно сохранены первый CID цели и последний refresh CID перед completion; времена 21,302 с и 3,963 с относятся к разным endpoints.
