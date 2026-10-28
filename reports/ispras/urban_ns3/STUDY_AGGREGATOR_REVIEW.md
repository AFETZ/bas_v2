# Независимая проверка агрегирования до freeze

28.10.2026. Прочитаны `analyze_urban_study.py` и `STUDY_ANALYSIS_METHOD.md`;
исправления проверены на worker commit `e3410ecc54b28d9a0954e353cc003819dcd8be33`.
Reviewer не создавал агрегатор. Новых симуляций, builds и экспериментальных результатов здесь нет.

## Проверенная методика

- Primary outcomes считаются на общем frozen горизонте, по независимым запускам/seed blocks.
- Fixed/failover differences — внутри радиорежима; interaction требует четыре случая одного блока и одного scenario/parameter variant.
- Bootstrap пересчитывает целые paired/block differences; пакеты/пять аппаратов/эпизоды не увеличивают n.
- N=5 exact3125 bootstrap-resamples — вычислительная точность перебора, не доказательство95% покрытия.
- Auxiliary n3/n1 остаются описательными; complete-pair интервалы условны по наблюдаемым парам.
- Отсутствующие planned попытки сохраняются строками. Missing bounds ограниченных outcomes — worst/best диапазоны, не CI и не записанные вместо пропуска наблюдения.
- Failed cleanup после полного записанного окна не стирает измерение; crash/короткий followup не выдаётся за полное наблюдение.
- `min(T_first_post_NLOS_UART,60)` — дополнительный bounded loss, не обещание восстановления за60с. NLOS не доказывает отказ доставки.
- Endpoint после direct-probe timeout использует отдельно наблюдаемый protocol detection. UART не объединяется с network receipt или физическим completion.

## Выявленные пробелы и исправления

1. Исходная версия доверяла block_id и могла связать различные RNG/horizon/config или смешать варианты interaction.
   `validate_frozen_blocks` теперь проверяет точную factorial grid четырёх main-случаев,
   общий RNG/horizon/scenario/parameter variant, одинаковый configSHA внутри каждой policy-пары,
   глобальные source/image и точные auxiliary пары. Повтор/недостаток planned ячейки — ошибка design;
   отсутствующий результат валидной planned ячейки — сохранённый missing outcome.
2. Проверка бинарника только до lock не идентифицировала post-build executable.
   Runner patch `b50d620` сохраняет SHA256 после build/reuse под принадлежащим интегратору runtime lock.
   Агрегатор требует совпадения `environment.native_binary_sha256` с freeze; несовпадение/пропуск
   сохраняются как provenance error, attempt остаётся в таблице без объединения с основной серией.
3. До первого main-запуска дополнительно проверить уникальность RNG seed/run между независимыми
   main block IDs одного варианта и равенство физических/маршрутных/model-параметров между geometry
   и LOS-only configs, кроме объявленной radio-абляции и неисполняемых подписей.
   Одни лишь разные имена block_id или одинаковая строка nominal этого не доказывают.

Независимо выполнены все14 focused tests исправленной версии; passed14/14.
Включены случаи malformed freeze, native identity mismatch, crash/cleanup, missing data,
auxiliary cohorts, четыре случая interaction и детерминированный bootstrap.
Запуск выполнялся обычным Python через importlib и временные каталоги, без pytest/симуляторов:

```bash
python3 - <<'PY'
import importlib.util, inspect, tempfile
from pathlib import Path
p = Path('network/tests/test_urban_study_analysis.py')
s = importlib.util.spec_from_file_location('review_tests', p)
m = importlib.util.module_from_spec(s)
s.loader.exec_module(m)
for name in sorted(n for n in vars(m) if n.startswith('test_')):
    fn = getattr(m, name)
    with tempfile.TemporaryDirectory(prefix='bas-study-review-') as directory:
        fn(Path(directory)) if inspect.signature(fn).parameters else fn()
    print('PASS', name)
PY
```

Вывод: методика и проверенные исправления подходят для frozen исследования при соблюдении
перечисленных design-invariants. Это review кода/методики; достоверность ещё не полученных
основных результатов, фактическую полноту raw и итоговые численные эффекты оно не подтверждает.
