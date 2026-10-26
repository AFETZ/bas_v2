# Подготовленная среда BAS live/leader

Подготовка: 26.10.2026. План: `doc/LIVE_LEADER_PLAN.md`. Стартовая задача: `NEXT_CHAT_PROMPT.txt`.
Это готовая организация разработки и исходная база; новый live/relay продукт ещё не реализован и не испытан.

## Исходная база

- Оригинал: `/home/bas/bas_v2-main`, ветка `fix/time-sync-20261026`, HEAD `304b9ef`.
- В изолированный snapshot `c56330f` перенесены 22 изменённых tracked файла и 2 untracked файла. Содержимое изменённых файлов сверено побайтово; исходный status/diff сохранён.
- Резервная запись: `/home/bas/BAS_ISPRAS_2026/dev_setup_20261026/`, включая patch, untracked и `snapshot.json`.
- Snapshot содержит уже существовавшие исправления времени, не новый подтверждённый эксперимент. Не приписывать его изменения новой команде агентов и не выдавать прежние результаты за новые.
- Продуктовый код после snapshot при подготовке не менялся; добавлены план, task-specific AGENTS, навыки и конфигурация агентов.

## Рабочие копии

| Каталог | Ветка | Назначение |
|---|---|---|
| `/home/bas/bas_v2-paper-live` | `paper/live-leader-20261026` | Интеграция, единственный mutable build/runtime, итоговые артефакты |
| `/home/bas/bas_v2-paper-runtime` | `paper/runtime-20261026` | Исправление полёта, лёгкий radio, peer topology и общие launch/C++ изменения |
| `/home/bas/bas_v2-paper-relay` | `paper/relay-20261026` | Прикладной координатор, lease, focused tests |
| `/home/bas/bas_v2-paper-metrics` | `paper/metrics-20261026` | Логирование/экспорт/графики на сохранённых raw |

Все worker-ветки начинаются с одинаковой подготовленной интеграционной версии. Перед очередной задачей root сообщает актуальный baseline и безопасно синхронизирует worker после интеграции прежней работы; не reset чужих незакоммиченных изменений.

## Runtime и зависимости

В интеграционную копию отдельно скопирован существующий `.external/ns-3-sionna-native` (около 6,3 GiB), включая установленный pinned source/build/dependencies. Это частная копия, не symlink в исходный mutable build. Копирование не доказывает работоспособность в новом каталоге; первым шагом проверить absolute build paths и выполнить только необходимую целевую пересборку.

Worker worktrees не получают общих writable build/install/.external. Их unit tests работают независимо; full-stack и native tests выполняет интегратор после переноса изменений. Pinned Docker image переиспользуется; новый образ нужен только при реальном изменении зависимостей.

Rock assets находятся в проекте. Town01 требует существующего внешнего bundle/подготовленных assets и не является обязательной второй картой. Старые raw для чтения: `/home/bas/bas_v2-main/runs/time-sync-20261026/`, `/home/bas/bas_v2-main/runs/native-radio-lockstep/`, `/home/bas/bas_v2-rc1/runs/`. Не писать результаты нового запуска в эти каталоги.

Глобальная эксклюзивная аренда симулятора и native build:

```bash
cd /home/bas/bas_v2-paper-live
flock --nonblock --conflict-exit-code 75 /tmp/bas-paper-runtime.lock <command>
```

Код 75 означает занятость. Порты, netns/TAP, контейнер и CPU/GPU могут конфликтовать между worktrees. Не удалять lock, не запускать чужой cleanup/stop. Команда внутри lock должна дождаться окончания runtime; не отпускать lock, оставив фоновый стенд.

Существующий пример оператора после минимальной проверки окружения (не запускался в этой копии):

```bash
flock --nonblock --conflict-exit-code 75 /tmp/bas-paper-runtime.lock \
  env BAS_NATIVE_PROPAGATION_PROFILE=friis BAS_NATIVE_CAPTURE_IMAGES=0 \
  make operator DEMO_SCENARIO=rock_demo DEMO_GUI=0 OPERATOR_DURATION=600
```

Владельцу этого же запуска: `make gcs` в отдельном терминале, завершение `make stop`. Не считать эти команды доказательством успешного полёта. Текущая пятибортовая физика требует ремонта P1.

## Навыки и custom agents

Канонические skill-файлы: `/home/bas/.codex/skills/bas-*/SKILL.md`. В каждой копии `.agents/skills/` содержит symlink на эти каталоги: современные Codex обнаруживают repo skills здесь. Из другого интерфейса можно указать абсолютный путь навыка в задаче.

- `bas-orchestrate`: root, владение, интеграция и приоритеты.
- `bas-live-runtime`: полёт, native radio и реальные часы.
- `bas-relay-handover`: независимый peer path, relay, lease и виды отказов.
- `bas-metrics`: параметры, корректные часы/высоты и продуктовые графики.
- `bas-experiments`: frozen paired campaign и анализ для статьи.
- `bas-review`: независимый анализ поведения, обходов и сырых данных.

Подготовленная `.codex/config.toml` задаёт до трёх одновременно открытых помощников. `.codex/agents/` содержит шаблоны `bas_runtime`, `bas_relay`, `bas_metrics`, `bas_experiments`, `bas_reviewer` и прежнюю универсальную leaf-роль. Модели, effort и permissions наследуются от родителя; глобальные настройки не менялись. Основной проверенный способ работы в текущем интерфейсе — обычные subagents с точным путём SKILL.md, назначенной рабочей копией и владением файлами. Они уже использовались для независимого аудита и подготовки комплекта.

Текущий открытый сеанс не обязан перечитать project config. Начать новый чат в интеграционном каталоге и передать NEXT_CHAT_PROMPT.txt. Прямые agent tools текущего сеанса использовались при подготовке, но это не проверка будущих custom roles в вашем интерфейсе.

Проверка через локальный app-server обнаружила все шесть навыков без ошибок. При этом project config отключён: установленный CLI сообщает о необходимости доверия к общему Git-каталогу `/home/bas/bas_v2`. Глобальный trust не расширяли; однократный `-c projects...trust_level` не снял это ограничение. Не утверждать, что custom-role TOML автоматически загружаются. Для обычного CLI-сеанса доступны документированные явные настройки агентов:

```bash
codex -C /home/bas/bas_v2-paper-live \
  -c agents.enabled=true -c agents.max_concurrent_threads_per_session=3
```

Для нового чата достаточно открыть интеграционный каталог и передать NEXT_CHAT_PROMPT.txt: он требует обычные subagents с навыками при отсутствии custom roles. Поддержка TOML-ролей остаётся дополнительной возможностью интерфейса, не зависимостью реализации BAS. [Официальная документация о project trust](https://learn.chatgpt.com/docs/config-file/config-reference).

## Интеграционная дисциплина

Root назначает конкретные файлы. Общие C++/runner/preflight редактирует один runtime worker, root переносит изменения последовательно. Relay и metrics описывают hooks/контракт событий и не правят эти файлы параллельно. Agents сохраняют завершённые изменения небольшими commits; root проверяет diff, затем интегрирует и выполняет минимальные значимые проверки.

Не добавлять отдельный CI/backend/Grafana/оркестратор ради организации агентов. Имеющихся collaboration tools, git worktrees, flock и текущих сценариев достаточно. Критические fixtures/protocol tests допустимы, но синтетические ACK не попадают в интегрированный опыт.

Воспроизводимость: план и источники работы находятся в репозитории; skills пока локальные. При переносе на другую машину скопировать шесть skill-каталогов и восстановить `.agents/skills` ссылки либо разместить их содержимое там. Не включать сторонние карты, секреты или многогигабайтные raw в git.
