# Анонимное дополнение: геометрическое NLOS и горизонтальный осмотр

Основной новый черновик — `src/manuscript.md`: сокращённое введение/обзор, новые
разделы 3–4, отдельные проверенные old30/sensitivity18 и новая городская серия.
`src/supplement.md` сохраняет промежуточное дополнение разделов 3–6; флаг
`--supplement` выбирает его. `paper/src` и прежние DOCX/PDF не меняются.
Без manifest результаты новой городской серии явно обозначены
как **не представленные/не измеренные в новой основной серии**. Таблицы и картинки
не заполняются нулями, ожидаемыми эффектами либо результатами старых когорт.

Генератор повторно использует `configure()`/типографику из проверенного
`paper/tools/build_docx.py` и официальный DOTM как источник стилей. Он не запускает
старый builder и не импортирует тело, авторов или macros шаблона. Анонимный DOCX
создаётся в новом `paper/urban/build/`; PDF — там же. Microsoft-шрифты в документе
сохраняются; LibreOffice-образ использует Liberation replacements, как старый черновик.

Рендеры выполняются только после освобождения общего runtime slot. Из интеграции:

```bash
flock --nonblock --conflict-exit-code 75 /tmp/bas-paper-runtime.lock \
  bash paper/urban/render_document.sh
# С фактическими результатами, до рендера подготовить описанный ниже manifest:
flock --nonblock --conflict-exit-code 75 /tmp/bas-paper-runtime.lock \
  bash paper/urban/render_document.sh paper/urban/results-manifest.json
```

Используется готовый `bas-paper-doc-tools:20261027`; не собирать его заново, если
зависимости не менялись. Renderer не останавливает чужие контейнеры. Код75 — занято.
Полные raw/read-only analysis доступны через отдельный read-only mount репозитория.

Manifest содержит `kind`: `preparation`, `engineering_pilot`, `partial_series`
или `frozen_series`; `source_commit`; `freeze_path` для frozen; `methodology`
(список четырёхстрочных строк таблицы: фактор/значение/clock/подтверждение);
`analyses`: список `{label,path,group}` к настоящим `urban_analysis.json`;
`figures`: список `{path,caption_ru,caption_en,measured:true}`;
`tables`: список `{csv,caption_ru,caption_en}` к готовым численным таблицам.
Дополнительно `study_json` указывает на реальный `urban_study.json` агрегатора.
Он добавляет все доступные per-run exports через их `input_paths.analysis`;
planned/failed/missing строки остаются в агрегированном представлении. Таблицы
содержат число valid окон, средние показателей, парные эффекты, interaction и
отдельные worst/best bounds пропусков. CI переносятся из агрегатора и не
пересчитываются в builder. `per_run_table:false` сохраняет все строки в CSV/JSON,
не раздувая основной текст таблицей из56 строк. Без `study_json` эффект серии не
заполняется. `build/presentation_tables.json` фиксирует ровно показанные значения.
Пути разрешаются относительно manifest; абсолютные пути допустимы и остаются
только во внутреннем build manifest, не в тексте DOCX. Label выбирать анонимный U01…
Поле `group` должно различать pilot/main/sensitivity/boundary; когорты не объединяются.

Таблица по каждому run показывает его статус, политику/радиорежим, timely/offered,
событийный UART endpoint и проверенные точки/план. Имена пользователей/локальные
пути не подставляются в рукопись. Crash и цензурирование имеют разные надписи.
Дополнительные CSV переносятся без самостоятельного подбора статистических оценок.
Каждая включённая картинка требует `measured:true` и хотя бы один analysis input.
Это маркировка источника; истинность изображения и статистики проверяет reviewer.

`--final` допускается лишь с `kind=frozen_series`, существующим freeze и настоящими
analysis inputs. Он отклоняет неподтверждённые физические completion и data-quality
ошибки в заявленных полных окнах; неполные crashed attempts остаются наблюдениями
с пропусками. Он не требует выигрыша failover, отсутствия failed или всех восстановлений.
Хэши источника, шаблона, входных данных/рисунков и DOCX пишутся в `build_manifest.json`.
Связь с raw сохранена в самих analysis; недостающие исходники не заменяются.

Тяжёлый рендер во время live не выполнялся автором подготовки. Рецепт требует
проверки DOCX/PDF/пагинации в интеграции после измерений. Сборщик не публикует статью.

Методика в новом тексте приведена к freeze4d96d19: 27 waypoint, подготовительный
relay70м, горизонтальное вхождение в тень на60м, последующий фасадный12м; барьеры
используют только доставленную телеметрию. При критическом событии follower в
основном удерживают точки. Physical hold2source s и route hold3boot s различаются.
Установленные Sionna-пакеты/fork остаются в образе, новый канал их не вызывает.
Хэши прочитанных frozen/model/review источников — `preparation_sources.json`;
новые main результаты туда не копировались.

Лёгкая проверка presentation transforms, без Docker/Matplotlib и runtime:

```bash
python3 -m unittest discover -s paper/urban -p 'test_study_inputs.py' -v
```

После завершения запуска/реального агрегирования подготовить manifest:

```bash
python3 paper/urban/prepare_manifest.py --study FINAL_AGGREGATE/urban_study.json \
  --freeze runs/urban-ns3-20261028/freeze.json --output paper/urban/results-manifest.json
```

Реальные графики/подписи добавить после построения по контракту `FIGURES.md`.
Утилита не рисует пустые графики и не создаёт результаты отсутствующих запусков.
