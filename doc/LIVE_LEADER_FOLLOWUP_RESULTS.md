# BAS: параметры, оператор, границы полезности переключения

27.10.2026. Основные 30 запусков `8e16ec2` сохранены без повторения. Отдельные 18 попыток заморожены на `e6453c8e2a1ed4f6e4f9b16c224104e62e2df5d1`; анализ — `de14d55`. Town01/DART, пять настоящих ArduPilot SITL, native ns-3 ad-hoc/Friis. Native SHA256 обеих серий: `3d1c0e64ee3342acdb1c3629572fa89641971a0faee265f2a00a3ee3c89853a8`. Физика, MAC, lease, detection и срок команды не перенастраивались по результатам.

## Параметры и оператор

Штатные `PARAM_REQUEST_LIST/PARAM_VALUE` с `param_count/index`, адресным `PARAM_REQUEST_READ` пропусков, проверкой SYSID/COMPID и исходных value bytes FE/FD. Сохраняются CSV/JSONL/upstream raw/tlog, изменения и сравнение до/после; `PARAM_SET` не отправляется. Снимок индексно полный, но не атомарный. Старые 150 DataFlash PARM остаются **observed-only**.

В `paper-operator-headless-03` до/после: все пять COMPID1 × 1388 индексов 0…1387, пропусков и конфликтов нет; один LIST на UAV, дозапросы не потребовались. Независимый upstream replay подтвердил кадры, значения и 54 изменения. Наблюдались ARMING_SKIPCHK=0, COMPASS_USE=1, SIM_SPEEDUP=1. Реальная потеря параметров с последующим radio repair этим успешным опытом не проверена; bounded repair проверен отдельными протокольными тестами.

Через настоящие `make operator` → `make gcs` автоматизирован ввод: `vehicle 3`, `set link 3`, запрос потоков 24/33, GUIDED/ARM/takeoff16/guided20/BRAKE/LAND/disarm. Все шесть исходных кадров совпали по SHA256 на пути НПУ→master→follower→UART; допуск внутри lease. UAV3 world ENU z: 0,3405→20,3609→0,3405 м, XY≤0,0668 м; BRAKE 7,1 модельной с, vmax=0,0268 м/с. Другие четыре на земле. Окно 75,191 model / 75,320446 wall с, RTF=0,998281. Операторский launcher сохранил код 2 после явного owned `docker stop` с кодом 0.

Снимки оператора заняли 145,043/144,115 внешних с. Пять независимых upstream UDP links новой серии читаются параллельно, вне измерительного окна: проверочный headless pilot дал 29,044/29,086 с на родительских этапах. Никакого обхода сети или ускорения автопилота.

## Новая серия чувствительности

Selective 0,5/2/10 с × fixed/failover × три whole-run пары, RngSeed=1/RngRun=1…3. Старты 43,060097/56,163750/43,813252 model с общие для пары и длительностей повтора; start/order seeds 20261027/20261028. Каждое окно 180 model с, 900 offered, цели каждые 20 model с, refresh 1 Hz wall. Версия, среда, порядок, seeds и бинарный файл заморожены до начала. Все тяжёлые работы выполнялись последовательно под `/tmp/bas-paper-runtime.lock`.

Все **18 окон полны**, 15 запусков completed, три failed после окна. Статус завершения, сетевое наблюдение и полнота параметров учитываются отдельно. Средние ниже используют все три допустимых окна ячейки, включая failed; n=3, без объединения с основной серией.

| Номинальный обрыв, с | Политика | Timely/offered, % | UART recovery, с | Новая физическая цель, с | Completed / failed |
|---:|:---|---:|---:|---:|:---:|
| 0,5 | fixed | 98,704 | 1,261 | 18,603 | 2 / 1 |
| 0,5 | failover | 98,926 | 1,271 | 18,613 | 3 / 0 |
| 2 | fixed | 97,963 | 3,602 | 19,168 | 2 / 1 |
| 2 | failover | 98,037 | 3,943 | 18,624 | 2 / 1 |
| 10 | fixed | 93,111 | 11,617 | 20,953 | 3 / 0 |
| 10 | failover | 97,704 | 3,939 | 18,615 | 3 / 0 |

При 0,5 с новых назначений нет. При 2 с все три активации произошли уже после RFend и свежего ответа старого канала: признак потенциально лишней смены. Парный эффект UART **+0,341 с**, разведочный 95% percentile bootstrap [0,002; 0,790]. При 10 с эффект **−7,677 с** [−7,997; −7,255], timely **+4,593 п.п.** [4,343; 4,778]. Физический эффект −2,338 с зависит от фазы выпуска цели; он не равен скорости переключения.

Из шести смен только четыре уложились в 4 с. Сохранены **4,123131166 с** (`paper-sensitivity-d02000ms-failover-s1-r03`, failed позднее) и **4,134810747 с** (`paper-sensitivity-d10000ms-failover-s1-r03`, completed). Порог 4 с относится к changed-relay UART, не к физической цели. Три пары дают разведочные интервалы: 27 упорядоченных bootstrap-ресэмплов, без гарантии покрытия или временной гарантии.

RF-длительности номинальные: одна fixed-попытка 0,5 с фактически дала **0,450658483 с**, fixed 10 с — 9,999869…10,049998 с. Дискретность phase polling 50 мс и фаза входят в эффект; actual RF bounds/парные различия/возрасты heartbeat-probe, renewal, выпуска команд сохранены в CSV/JSON. Настройки после просмотра не подгонялись.

Failures: order10/11 — clock coupling fail-closed во время post-выгрузки, через 36,142/62,188 с после конца окна; order12 — при LAND через 25,729 с. В 10/11 все пять LAND/disarm подтверждены; в 12 только UAV1. Причина замедления источника по нагрузке/рендеру/планировщику не разделена. После трёх остановок возобновлены только ещё не начатые 13–18; повторов и перезаписей нет.

Параметры серии: pre — 18/18 полных пятибортовых снимков; post — 15 полных, два partial, один skipped. Независимо декодированы **165 полных таблиц**, 10 partial; пять post-таблиц не записаны. Независимые UART/physical/RF endpoints всех 18 совпали с анализом. В трёх partial-компонентах unindexed PARAM_VALUE имеет index=65535/None: пустое CSV-поле не стало нулём или полным индексом; различие `str(None)`/пустой строки в первом audit отдельно разобрано. Непарные отсутствующие значения не засчитаны изменением параметра.

## Разложение прежних результатов и GUI

Старая selective-когорта, n=5: failover T_goal=14,972628 ожидания новой цели +0,972830 до создания команды +0,010541 command→UART +5,346194 движения/hold =**21,302193 с**. Fixed: 54,968841+7,376517+0,010038+5,359328=**67,714724 с**. Command→UART включает обработку/relay, а не только RF airtime. Первый CID цели и последний refresh CID сохранены раздельно; UART recovery **3,963387 с** — другой endpoint.

Настоящий GUI `paper-gui-overhead-07`: Town01 и пять стоящих моделей, viewport 960×480, viewer quota 0,75 CPU, прогрев 45 с. Фактическое окно 60,009419 wall с; observed clocks 59,897 model / 59,964692 wall с, RTF=**0,998871**, p5 0,997549; lag p95/max=2,492/6,859 мс. Все 2614 clock samples имеют пять fresh UAV; 225/225 nonce-ответов. Viewer CPU mean=74,63% одного ядра, RSS max=2 027 003 904 байт; FPS/graphics GPU memory не измерены. Это наземный опыт, не полёт с GUI и не причинный headless/GUI benchmark. Предыдущие неуспешные GUI/parameter pilots сохранены отдельно и не включены в 18.

## Файлы и воспроизведение

- [Анонимный русский PDF, 12 страниц](../paper/build/manuscript_anonymous.pdf), [DOCX](../paper/build/manuscript_anonymous.docx), [проверки артефактов](../paper/build/artifact_checks.json). Официальные стили, 10 пт, RU/EN подписи, 11 первичных источников; PDF использует Liberation Serif/Sans вместо отсутствующих TNR/Arial. Научный черновик, на конференцию не отправлен.
- [Компактные CSV/итоги](../paper/data/), [графики](../paper/figures/), [снимок и GUI-графики](../paper/evidence/gui/), [freeze/журнал/независимый audit/команды](../paper/evidence/followup/).
- Полные local raw: `runs/native-radio-realtime/paper-sensitivity-*`, `paper-operator-headless-03`, `paper-gui-overhead-07`. Локальные отчёты: `runs/paper-followup-20261027/sensitivity/{analysis,figures}`, `sensitivity-independent-review`, `gui-overhead07-analysis`; все per-run trajectory/targets/RTF/lag/resource CSV находятся в `sensitivity/analysis/runs/<id>/live/metrics/`. Полные трассы, assets и Docker images не включаются в Git.
- Оригинальный `bas_v2-main`: HEAD/status/binary diff/cached diff и SHA256 обоих untracked совпали с исходным snapshot. Старые raw сохранены. Свои симуляторы остановлены. GitHub push рабочей ветки явно разрешён последующим запросом пользователя; статья не отправляется.

```bash
cd /home/bas/bas_v2-paper-live
( source paper/evidence/followup/reproduce-one.env
  BAS_NATIVE_FIVE_RUN_ID=user-selective10-01 flock -n -E 75 /tmp/bas-paper-runtime.lock ./network/ns3/run_native_radio_five_uav.sh
)
# Отдельный ручной запуск; fresh RUN_ID, настоящий MAVProxy во втором терминале.
( source paper/evidence/followup/reproduce-one.env
  BAS_NATIVE_FIVE_RUN_ID=user-operator-01 BAS_NATIVE_FAULT_PROFILE=none flock -n -E 75 /tmp/bas-paper-runtime.lock make operator DEMO_SCENARIO=town01 DEMO_GUI=0 OPERATOR_DURATION=900
)
make gcs
# Сначала LAND и подтверждённый ground/disarm; timeout оператора не сажает.
docker stop --timeout 120 bas-v2-native-radio-five-uav
```

Подробные команды анализа, параметров и документа — [evidence README](../paper/evidence/followup/README.md). Лёгкий runtime не требует Sionna/GPU/OptiX; native GUI отдельно использует графику. Friis не моделирует затенение зданиями; RF-HOLD предполагает живые sidecar/runtime. Общие часы одного хоста и вертикальная миссия ограничивают переносимость результата.
