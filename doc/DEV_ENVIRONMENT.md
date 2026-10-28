# BAS paper: текущая среда разработки

Обновлено29.10.2026: городской follow-up измерен отдельно на `4d96d19`;
56 попыток,55 полных окон,36 completed/20 failed. Сводка и границы —
`reports/ispras/urban_ns3/RESULTS.md`, raw — `runs/native-radio-realtime/urban20261028-*`.
Новая сцена генерируется из JSON: общие объёмы Gazebo/ns-3, native Buildings/
LogDistance, WP_SPD3, настоящие пять SITL. Sionna RT не вызывается; pinned image
сохраняет старые build-пакеты. Town01/main30 ниже остаётся исторической базой.

Городские копии: `bas-urban-runtime` (native), `bas-urban-mission` (миссия/текст),
`bas-urban-metrics` (анализ/рисунки), `bas-urban-report` (root reproduction inputs).
После56 по прямому указанию пользователя созданы `bas-urban-astra-{analysis,review,text}`
с GPT-6 Astra Ultra; reviewers не являются авторами проверяемой части.
Heavy builds/runs/render/hash идут только из интеграции последовательно под
прежним runtime lock. Worker не получает writable external/build и не повторяет
опыты. Исходная `bas_v2-main` и прежние raw сохранены.

Аудит 28.10.2026 завершён в `reports/ispras/README.md`: восемь новых вертикальных
диагностических запусков, два preflight failed сохранены; никаких runtime/algorithm
изменений. Слова planned/blockers этого аудита относятся к состоянию до отдельного
городского расширения; прежние измерения не становятся obstacle-aware задним числом.
Новые audit worktrees: `/home/bas/bas-audit-{algorithm,metrics,obstacles}` от `bb026a0`;
они не содержат собственных writable native builds/assets и не заменяют интеграцию.

Обновлено 27.10.2026. Задача: `NEXT_STEP_PROMPT.txt` и текущий запрос пользователя.
Прочитать `LIVE_LEADER_REVIEW_20261027.md`, `LIVE_LEADER_RESULTS.md`, `network/STATUS.md`.
`LIVE_LEADER_PLAN.md` сохраняет исторический план реализации; P0–P5 не начинать заново.

## Проверенная база

- Интеграция: `/home/bas/bas_v2-paper-live`, ветка `paper/live-leader-20261026`.
- Основная серия: 30 запусков на `8e16ec223e895c11891e6e7d7c39dff8cee39cac`; reviewed HEAD `4800d98` отличается только документацией. Результаты независимо проверены; raw не изменять.
- Рабочий профиль: Town01, stock DART/Gazebo, пять ArduPilot SITL, native ns-3.48 ad-hoc/Friis, control UART, metrics_only, native capture в tmpfs с публикацией после остановки.
- Реально доказаны пять физических циклов, relay с четырьмя follower, selective handover, HOLD при RF-изоляции и отдельные 600 model s. Новый этап завершил make operator/make gcs цикл UAV3 и полные pre/post-параметры; точные границы — `LIVE_LEADER_FOLLOWUP_RESULTS.md`.
- Friis не даёт геометрического затенения; Sionna RT/GPU/OptiX, новая карта и ускорение не входят в этап. Rock/Bullet подъём не подтверждён.
- Image: `sha256:89d78eff9914b1644a1b01b793612e9ee8b19916c5a7bf5f578ba6d8bfbfafe5`. Пересборка образа без изменения зависимостей не нужна.
- Частные `.external/ns-3-sionna-native`, `.python-deps-py310`, build/install и Town01 assets находятся в интеграции. Worker не получает writable symlink на них.
- Host Python не имеет всех пакетов; offline analysis использует pinned image/Python3.10. Точный argv основной серии: `runs/paper-live-20261026/campaign/analysis-command.json`.

## Сохранность и рабочие копии

Оригинал `/home/bas/bas_v2-main` остаётся на `304b9ef` с прежними локальными изменениями.
Подготовительный snapshot `c56330f` перенёс 22 tracked и два untracked файла; резервные status/diff/bytes: `/home/bas/BAS_ISPRAS_2026/dev_setup_20261026/`.
Не сбрасывать оригинал и старые worker-ветки. Они имеют разные HEAD и не являются текущей интеграцией.

| Каталог | Назначение |
|---|---|
| `/home/bas/bas_v2-paper-live` | root, единственный native build/full runtime, артефакты |
| `/home/bas/bas_v2-paper-runtime` | прежняя runtime-ветка, сохранить |
| `/home/bas/bas_v2-paper-relay` | прежняя relay-ветка, сохранить |
| `/home/bas/bas_v2-paper-metrics` | прежняя metrics-ветка, сохранить |
| `/home/bas/bas_v2-paper-params-next` | новый worker от актуальной интеграции: MAVLink parameters |
| `/home/bas/bas_v2-paper-study-next` | новый worker: sensitivity/decomposition/plots |
| `/home/bas/bas_v2-paper-text-next` | новый worker: анонимная статья/официальный шаблон |

Root создаёт новые worktrees от зафиксированной актуальной версии. Каждый worker получает абсолютный путь skill, cwd, владельцев файлов и минимальную проверку. Только собственные завершённые commits переносятся последовательно; старые ветки целиком не сливать.

## Владение и runtime

- Root: интеграция, AGENTS/DEV_ENVIRONMENT/итоговый STATUS, операторский опыт, запуск серии.
- Runtime worker: parameter adapter/tests; общие runner/C++ только по явному согласованию root.
- Metrics worker: existing analyzers, sensitivity plan/controller и focused tests; общий runner не редактирует.
- Text worker: `paper/` и references/figures/generation; продуктовый runtime не меняет.
- Независимый reviewer проверяет часть, которую не создавал. До трёх помощников; вложенные агенты не нужны.

Все тяжёлые builds/runs из интеграции, по одному:

```bash
cd /home/bas/bas_v2-paper-live
flock --nonblock --conflict-exit-code 75 /tmp/bas-paper-runtime.lock <command>
```

Код75 означает занятость: не удалять lock и не останавливать чужой процесс. Команда удерживает lock до остановки своего runtime и публикации raw. Worktrees не изолируют ports/TAP/netns/containers/CPU.
Общий runner/C++ имеет одного владельца. Worker unit tests не запускают global cleanup. DCC production containers не относятся к BAS.

## Навыки и запуск

Канонические skills: `/home/bas/.codex/skills/bas-{orchestrate,live-runtime,relay-handover,metrics,experiments,review}/SKILL.md`.
Обычные subagents с абсолютным SKILL.md/cwd/владением — проверенный основной способ. Custom-role TOML и project trust не должны блокировать работу. Модели/effort наследуются.
`.agents/skills/` содержит ссылки; перенос на другой хост требует сохранить skills или заменить ссылки содержимым. Не менять глобальный trust ради организации работы.

Команды рабочего профиля — `LIVE_LEADER_RESULTS.md`, `runs/paper-live-20261026/live-profile.env`.
Выбирать новый RUN_ID; параметры выгружать через тот же сетевой путь до/после измерительного окна.
Интерактивный timeout не выполняет LAND: сначала LAND, ground/disarm, затем остановка своего контейнера с grace120s.

Новая серия отдельно: 18 попыток на `e6453c8`, 15 completed/3 failed после полных окон; 0,5/2/10s selective × fixed/failover × три пары. Freeze/config/order/seeds сохранены, два handover >4s. До всех опытов полные пять таблиц; после — 15 полных снимков, два partial, один skipped. Старую серию не объединять; failures не повторять для замены результатов.
GUI07: отдельное наземное окно 60,009wall s, RTF0,998871 и настоящий снимок Town01/пяти моделей; полёт с GUI не заявляется. Анонимный черновик 12 страниц — `paper/build/`; document image `bas-paper-doc-tools:20261027` не меняет runtime image.
Не добавлять новый backend/CI/оркестратор. Последующий запрос пользователя явно разрешил GitHub commit/push рабочей ветки. Статью на конференцию не отправлять; full raw/assets/images остаются вне Git.
