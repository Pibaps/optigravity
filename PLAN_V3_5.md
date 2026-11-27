# Plan d'Exécution V3.5 — Automatisation Complète des Tests (`ogy` TUI, ACP, MCP, CLI & E2E)

> **Sources consolidées** : [Automatisation Tests TUI Optigravity.pdf](Automatisation%20Tests%20TUI%20Optigravity.pdf), [PLAN_V3_1.md](PLAN_V3_1.md) (Isolation `ogy`/`agy` & Backup `.gemini`), [PLAN_V3_PROFESSIONNALISATION.md](PLAN_V3_PROFESSIONNALISATION.md) et [pyproject.toml](pyproject.toml).
>
> **Objectif** : éliminer les validations manuelles répétitives grâce à une **chaîne automatisée et déterministe à 4 étages** qui n'invoque jamais le binaire réel `agy` ni les quotas OAuth, **100 % compatible avec l'architecture d'isolation V3.1** (`OGY_ACTIVE=1`, `profile_mode = "isolated"`, `ogy backup` / `ogy restore`).

---

## 0. Invariants & Harmonisation V3.1 ↔ V3.5

Avant d'implémenter chaque lot, respecter ces **5 garde-fous transversaux** :

1. **Zéro pollution du `~/.gemini/` développeur (Héritage V3.1)** : un garde-fou global dans `tests/conftest.py` isole systématiquement `OGY_DB_PATH`, `OGY_GEMINI_HOME`, `HOME` / `USERPROFILE` dans `tmp_path` afin qu'aucun test (ni `pytest-xdist`, ni `mutmut`) ne puisse modifier le vrai `.gemini` de la machine.
2. **Test explicite des deux régimes V3.1 (`OGY_ACTIVE=1` vs absent)** : `isolate_hooks = true` étant actif par défaut dès V3.1, toute fixture de test de hooks/statusline/firebreaks doit explicitement déclarer `OGY_ACTIVE="1"` pour tester le chemin actif, et des tests dédiés doivent vérifier le *pass-through* natif (`{"decision": "allow"}`, 0 écriture `ogy.db`, statusline vide) lorsque `OGY_ACTIVE` est absent.
3. **Chemin chaud V1 100 % stdlib préservé** : toutes les nouvelles dépendances de test (`pytest-textual-snapshot`, `time-machine`, `inline-snapshot`, `dirty-equals`, `syrupy`, `pytest-xdist`, `pytest-timeout`, `pytest-cov`, `diff-cover`, `pytest-testmon`, `tui-test` / `pywinpty`, `mutmut`) sont strictement confinées à `[project.optional-dependencies].dev` (ou groupes de dev `uv`) — **0 dépendance ajoutée au runtime**.
4. **Scénarios déterministes plutôt qu'agent testeur autonome** : conformément à la littérature TUI récente (*Chao Peng et al., `tuicov`/`tuibot`*), privilégier des scénarios scriptés déterministes, des contrats protocolaires et des snapshots SVG ciblés plutôt qu'une exploration LLM non déterministe.
5. **Règle d'ingénierie anti-régression** : chaque bug constaté une fois doit se traduire par un nouveau scénario du Fake ACP Agent ou un test Pilot / `time-machine` / V3.1 isolation ; aucune vérification manuelle déjà effectuée ne doit être à refaire.

---

## Architecture Cible à 4 Étages (Harmonisée V3.1)

```text
┌────────────────────────────────────────────────────────────────────────────┐
│ Étage 4 : Terminal E2E Cross-OS (tui-test / PTY & ConPTY)                  │
│  • Spinner TTY vs pipe (ogy wrap), délégation TTY (ogy run), ogy tui live  │
│  • Coexistence simultanée V3.1 : ogy (--isolated / --settings) vs agy pur  │
├────────────────────────────────────────────────────────────────────────────┤
│ Étage 3 : Snapshots Visuels SVG (pytest-textual-snapshot, ubuntu-latest)   │
│  • Rendu styles.tcss, Modales, Sidebar, tailles 80x24 & 140x40             │
│  • Seule action humaine résiduelle : valider un diff SVG en PR si voulu    │
├────────────────────────────────────────────────────────────────────────────┤
│ Étage 2 : TUI Headless & Protocoles Réels (Pilot + Fake ACP + MCP stdio)   │
│  • Fake ACP Agent stdio (JSON-RPC 2.0) validant aussi env V3.1 (OGY_ACTIVE)│
│  • MCP in-memory + pureté stdout & env isolé V3.1 (ogy-mcp, ogy-subagents) │
├────────────────────────────────────────────────────────────────────────────┤
│ Étage 1 : Noyau Déterministe (pytest + time-machine + Fake GenAI + V3.1)   │
│  • SQLite WAL, Pricing (2026-12-01 / 2027-01-01), Router, Firebreaks       │
│  • Contrats V3.1 : OGY_ACTIVE, backup/restore SHA-256, profil isolé        │
└────────────────────────────────────────────────────────────────────────────┘
         ▲                              ▲                              ▲
   Local (testmon + Justfile)     PR Rapide (Ubuntu + diff-cover)   Matrice 3 OS + Hebdo mutmut
```

---

# PHASE 1 — Cœur TUI, Fake ACP Agent stdio & Contrats V3.1

**Objectif** : supprimer tout lancement manuel d'`agy acp` et toute consommation de quotas OAuth pour valider `ogy tui`, tout en vérifiant de bout en bout la plomberie V3.1 (`OGY_ACTIVE=1`, `--settings`, profil isolé).

### Lot 3.5.1 — Fake ACP Agent Déterministe sur stdio (`tests/fixtures/fake_acp_agent.py`)
* **Dépendances ajoutées** : aucune (`textual` et `agent-client-protocol` sont déjà dans `[tui]`).
* **Fichiers** : `ogy_frontends/acp_tui/client.py`, `tests/fixtures/fake_acp_agent.py` (nouveau), `tests/conftest.py` (nouveau), `tests/test_acp_client.py`, `tests/test_tui_app.py`
* **Actions précises** :
  1. **Commande injectable dans `AcpClientConfig` (`client.py`)** :
     * Ajouter un champ optionnel `command_override: list[str] | None = None` dans `AcpClientConfig` afin que `AcpClient.connect()` puisse lancer `[sys.executable, "-m", "tests.fixtures.fake_acp_agent", "--scenario", "<nom>"]` via `acp.spawn_agent_process(...)` **tout en conservant intégralement la construction d'environnement et d'arguments V3.1** (`OGY_ACTIVE=1`, `AGY_CLI_DISABLE_AUTO_UPDATE="true"`, env profil isolé et `--settings` si configuré).
  2. **Implémentation du Fake ACP Agent (`tests/fixtures/fake_acp_agent.py`)** :
     * Basé sur `acp.Agent`, `run_agent`, `acp.schema` et les constructeurs d'événements (`start_edit_tool_call`, `update_tool_call`, `tool_diff_content`, `PermissionOption`).
     * Scénarios scriptables via `--scenario` :
       * `nominal_stream` : `AgentThoughtEvent` → appels de lecture regroupés (`ActivityGroup`) → streaming `agent_message_chunk` → `UsageEvent` vérifiant l'écriture SQLite `ogy.db` et les jauges sidebar.
       * `edit_with_diff_and_permission` : `tool_diff_content` + `request_permission` suspendant l'agent jusqu'à réception de la décision (`y`, `a`, `n`, `x`, `f` depuis `PermissionModal`), puis écho de la décision reçue.
       * `elicitation_and_tos` : requêtes `ElicitationModal` (mode `form` JSON Schema + mode `url` consentement) + vérification du rejet de `BANNED_ACP_MODES` (`yolo`).
       * `faults_and_cancel` : trame JSON-RPC corrompue, latence contrôlée pour tester `Ctrl+C` (`session/cancel`), et arrêt brutal du sous-processus pour vérifier la récupération propre de l'UI.
       * `v31_env_audit` *(Harmonisation V3.1)* : inspecte `os.environ` et `sys.argv` reçus par le sous-processus pour attester la présence de `OGY_ACTIVE=1`, l'isolement `OGY_GEMINI_HOME` / `USERPROFILE` en mode `isolated` et la transmission de `--settings` (sans jamais toucher au `~/.gemini/` hôte).
  3. **Verrouillage des 4 pièges Textual Pilot (`tests/conftest.py`, `tests/test_tui_app.py`)** :
     * **Factory `make_tui_app` (jamais `App.run_test()` dans une fixture `pytest-asyncio`)** : contourne l'incompatibilité Textual #4998 (`NoActiveAppError` / `ValueError: was created in a different Context`). La factory injecte une DB SQLite temporaire, une `OgyConfig` isolée V3.1 et le Fake ACP Agent stdio.
     * **Synchronisation déterministe du worker SQLite** : remplacer les `asyncio.sleep()` / pauses arbitraires par `await app.workers.wait_for_complete()` suivi de `await pilot.pause()`.
     * **Test de régression streaming + ancrage (`anchor()` / `release_anchor()`)** : calqué sur Textual PR #6694 (injection de chunks en streaming → scroll haut `y=-1` → vérification que `scroll_y` reste figé pendant l'arrivée de nouveaux chunks et que la pilule `↓ New messages · End` apparaît → `scroll_end()` / touche `End` réactive l'ancre).
     * **Test `paste-collapse` sans `pilot.paste()`** : poster directement un événement `events.Paste` sur `PromptTextArea` (≥ 8 lignes ou ≥ 600 caractères) → `await pilot.pause()` → vérifier la puce `[Pasted #1 · X lines]` et la réinjection complète à l'envoi.
* **Critères « Done »** : tous les parcours interactifs TUI + ACP JSON-RPC stdio tournent sans `agy` réel ni OAuth ; 0 `asyncio.sleep()` dans les nouveaux tests Pilot ; scénario `v31_env_audit` vert en modes `shared` et `isolated`.

---

# PHASE 2 — Snapshots Visuels SVG, Contrats MCP/GenAI, Temps & Sorties CLI

**Objectif** : éliminer les inspections visuelles CSS manuelles, la vérification manuelle du formatage CLI/HUD, les tests manuels de dates/quotas et les pannes silencieuses stdio/API.

### Lot 3.5.2 — Snapshots Visuels SVG (`pytest-textual-snapshot`)
* **Ajouts `[dev]`** : `pytest-textual-snapshot>=1.1.0` (compatible `pytest-xdist`).
* **Fichiers** : `tests/test_tui_snapshots.py` (nouveau), `tests/conftest.py`, `pyproject.toml` (marqueur `snapshot`)
* **Actions précises** :
  1. Utiliser `snap_compare(app, terminal_size=..., run_before=...)` en passant directement l'instance `OptigravityApp` non démarrée produite par `make_seeded_tui_app(scenario=...)` (base SQLite pré-remplie + Fake ACP Agent).
  2. **Neutralisation stricte du non-déterminisme dans `run_before`** :
     * Helper `disable_blink_for_active_cursors(pilot)`.
     * Horloge figée (`time-machine`, `tick=False`), IDs de session, chemins normalisés et hashes fixes.
     * `await pilot.app.workers.wait_for_complete()`, `await pilot.wait_for_scheduled_animations()`, `await pilot.pause()`.
  3. **Couverture ciblée sur les écrans structurants** (marqués `@pytest.mark.snapshot`) :
     * Écran d'accueil, streaming + sidebar télémétrie, `PermissionModal` avec diff coloré, `ElicitationModal`, `SessionPickerModal`, `HandoffReviewModal`.
     * Deux tailles contrastées : `(80, 24)` et `(140, 40)` (plus `(120, 36)` standard).
  4. **Discipline CI** : verrouiller la version de `textual` via `uv.lock`, restreindre la comparaison des golden SVGs à `ubuntu-latest` uniquement (artefact HTML visuel uploadé en cas d'échec), tout en exécutant tous les tests fonctionnels Pilot sur Windows, Linux et macOS.
  5. *(Optionnel doc)* : génération PNG headless via `App.export_screenshot()` + `resvg` (sans Cairo ni `librsvg`, compatible Windows).
* **Critères « Done »** : 0 faux positif sur 3 runs consécutifs sur `ubuntu-latest` ; `pytest --snapshot-update` régénère proprement les SVGs.

---

### Lot 3.5.3 — Serveurs MCP (`ogy-mcp`, `ogy-subagents`) & Harnais Gemini API (`429` / `402`)
* **Fichiers** : `tests/test_mcp_integration.py` (nouveau), `tests/test_subagents_phase3.py`
* **Actions précises** :
  1. **Tests MCP en mémoire (SDK `mcp>=2,<3`)** :
     * Connexion directe `async with Client(app) as client:` (sans sous-processus) pour valider en quelques millisecondes `list_tools()`, `call_tool()`, la compacité des schémas et `_DualProgressReporter` (`ctx.report_progress` + écriture SQLite `subagent_runs`).
  2. **Deux garde-fous sur les exécutables stdio réels (`ogy-mcp` et `ogy-subagents`)** :
     * **Pureté `stdout`** : spawner le vrai binaire, envoyer `initialize` JSON-RPC et vérifier que **chaque ligne** reçue sur `stdout` est un objet JSON-RPC valide sur une seule ligne (0 `print`, 0 log parasite).
     * **Isolation d'environnement (Harmonisé V3.1)** : lancer les binaires avec un `env` minimal et un `cwd` externe, avec `OGY_GEMINI_HOME` pointant vers un profil isolé `tmp_path`, et valider la résolution des chemins absolus, `config.toml`, la cascade `env → keyring → secrets.env` et **l'absence totale d'accès au `~/.gemini/` hôte**.
  3. **Harnais Gemini API déterministe (`google-genai`)** :
     * Injecter un faux client asynchrone remplaçant `client.aio.models.generate_content` levant les vraies exceptions SDK : `google.genai.errors.ClientError(status_code, response_json, response)` (`429`, `402`, `400`) et `ServerError` (`5xx`).
     * Vérifier : bascule Projet A Free → Projet B Tier 1 sur `429 RPD`, backoff exponentiel sur `rpm_tpm`, cooldown 15 min sur `rpd_ambiguous`, arrêt immédiat `SubagentBillingError` sur `402`.
     * Retourner des objets `types.Part` portant `thought_signature` pour prouver que la boucle Niveau 2 (`max_steps=8`, `store=False`) préserve et retransmet intégralement les signatures de pensée à chaque tour de *function calling*.
* **Critères « Done »** : 100 % des scénarios `429`/`402`/`thought_signature` et des contrats stdio MCP passent sans clé API réelle ni réseau.

---

### Lot 3.5.4 — Temps Déterministe (`time-machine`) & Snapshots CLI/Données (`inline-snapshot`, `syrupy`)
* **Ajouts `[dev]`** : `time-machine`, `inline-snapshot`, `dirty-equals`, `syrupy`.
* **Fichiers** : `tests/test_pricing_health.py`, `tests/test_hooks_statusline.py`, `tests/test_cli.py`, `tests/test_cli_snapshots.py` (nouveau)
* **Actions précises** :
  1. **Tests temporels avec `time-machine` (`tick=False` pour assertions exactes)** :
     * Bascules tarifaires `2026-12-01` (Carbon/Argon) et `2027-01-01` (doublement `gemini-3.6/3.8-flash`).
     * Reset du circuit-breaker RPD à minuit Pacifique (`23:59:59` → `00:00:01` `ZoneInfo("America/Los_Angeles")`).
     * TTL 30 s de `pending_tasks`, debounce 10 s du HUD statusline, purge LRU 7 jours du stash, seuils d'éviction cache (`150s`, `180s`, `300s`).
     * *(Harmonisation V3.1)* : nommage horodaté déterministe des snapshots `ogy backup` (`YYYYMMDD-HHMMSS`) et détection anti-doublon.
     * Prévoir une horloge injectable / paramètre configurable uniquement là où le code utilise `time.monotonic()` ou les horloges internes SQLite (non patchées par `time-machine`).
  2. **Snapshots de structures compactes (`inline-snapshot` + `dirty-equals`)** :
     * Payloads JSON de hooks (`pre-tool`, `session-start`, `stop`) dans les **deux régimes V3.1** (`OGY_ACTIVE="1"` actif vs `OGY_ACTIVE` absent *pass-through*), dataclasses normalisées d'événements ACP et réponses `ogy-mcp`.
  3. **Snapshots de sorties texte volumineuses (`syrupy`)** :
     * Tableaux `ogy stats` / `ogy waste`, variantes ANSI/`NO_COLOR` et multi-largeurs du HUD `ogy-hook statusline`, messages de refus `firebreaks.py` avec décompte de lignes, `.ogy/handoff.md` (≤ 30 lignes).
     * *(Harmonisation V3.1)* : sorties `ogy backup --list`, `ogy restore --dry-run` et `ogy doctor` (mode `isolated` vs `shared`, état du lien OAuth, intégrité backups), avec filtres `syrupy` sur les chemins temporaires, horodatages et UUID/hashes.
* **Critères « Done »** : 0 test dépendant de la date système ou du fuseau horaire ; sorties CLI/HUD et contrats V3.1 verrouillés par snapshots.

---

# PHASE 3 — Orchestration Zéro Friction (Local + CI GitHub Actions)

**Objectif** : rendre l'exécution des tests instantanée en local et entièrement automatique en CI (retour < 2 min sur PR, matrice 3 OS en continu, couverture différentielle bloquante).

### Lot 3.5.5 — Boucle Locale Incrémentale (`pytest-testmon`, `Justfile`) & Règles Agents
* **Ajouts `[dev]`** : `pytest-xdist`, `pytest-timeout`, `pytest-cov`, `diff-cover`, `pytest-testmon`.
* **Fichiers** : `pyproject.toml`, `Justfile` (nouveau), `.gitignore` (ajouter `.testmondata*`), `README.technical.md`
* **Actions précises** :
  1. **Marqueurs pytest (`pyproject.toml`)** : déclarer explicitement `snapshot` (SVGs Textual) et `e2e_tty` (tests en vrai terminal PTY/ConPTY).
  2. **`Justfile` cross-platform (Windows PowerShell + Linux/macOS)** adossé à `uv run -m pytest` (mode module recommandé avec `uv`) :
     * `just dev` : `uv run -m pytest --testmon -q --tb=short` (relance uniquement les tests impactés + échecs précédents).
     * `just tui` : `uv run -m pytest tests/test_tui_app.py tests/test_acp_client.py -q --tb=short`.
     * `just v31` : `uv run -m pytest -k "isolated or backup or restore or ogy_active" -q --tb=short`.
     * `just snap-update` : `uv run -m pytest -m snapshot --snapshot-update`.
     * `just all` : `uv run -m pytest -n auto --timeout=30 -q --tb=short`.
  3. **Convention agents** : documenter dans `README.technical.md` (et règles dépôt) l'exécution systématique de `uv run -m pytest -q --tb=short` avant toute fin de tâche.
* **Critères « Done »** : `just dev` tourne en quelques secondes après édition d'un fichier ; 0 divergence PowerShell vs bash.

---

### Lot 3.5.6 — Pipeline GitHub Actions Multi-Étages (`.github/workflows/ci.yml`)
* **Fichiers** : `.github/workflows/ci.yml` (nouveau)
* **Actions précises** :
  1. **Job `fast-gate-and-snapshots` (`ubuntu-latest`, `timeout-minutes: 15`)** :
     * `actions/checkout@v4` avec `fetch-depth: 0` (requis pour `diff-cover`), `astral-sh/setup-uv@v6` (`enable-cache: true`), `concurrency: { group: ci-${{ github.ref }}, cancel-in-progress: true }`.
     * `uv sync --all-extras --frozen`.
     * `uv run -m pytest -n auto --timeout=30 --cov=ogy_core --cov=ogy_cli --cov=ogy_hooks --cov=ogy_statusline --cov=ogy_mcp --cov=ogy_subagents --cov=ogy_frontends --cov-report=xml`.
     * Sur PR : `uv run diff-cover coverage.xml --compare-branch=origin/${{ github.base_ref }} --fail-under=85` (empêche l'introduction de code non testé sans exiger de réécrire l'existant).
     * En cas d'échec : `actions/upload-artifact@v4` du rapport HTML visuel `pytest-textual-snapshot`.
  2. **Job `cross-os-matrix` (`[ubuntu-latest, windows-latest, macos-latest]`, `fail-fast: false`, `timeout-minutes: 20`)** :
     * `uv sync --all-extras --frozen`.
     * `uv run -m pytest -m "not snapshot" -n auto --timeout=45` (exécute tous les tests fonctionnels, V3.1 isolation Windows/POSIX, MCP stdio et E2E TTY).
* **Critères « Done »** : PR verte en < 2 min sur `fast-gate-and-snapshots` ; matrice 3 OS valide les chemins Windows (jonctions NTFS, `USERPROFILE`, ConPTY) et POSIX (symlinks, `HOME`, PTY).

---

# PHASE 4 — Terminal Réel E2E Cross-OS & Mutation Testing Hebdomadaire

**Objectif** : fermer les deux derniers angles morts (comportements PTY/ConPTY natifs et assertions trop faibles sur les modules critiques).

### Lot 3.5.7 — Smoke Tests E2E en Vrai Terminal (`tui-test` / PTY & ConPTY)
* **Ajouts `[dev]`** : `tui-test` (et/ou `pywinpty` sous Windows ; écarter `pexpect.popen_spawn.PopenSpawn` qui n'offre pas de vrai TTY).
* **Fichiers** : `tests/test_e2e_tty.py` (nouveau, marqué `@pytest.mark.e2e_tty`), `tests/fixtures/fake_agy_cli.py` (nouveau)
* **Actions précises** :
  1. **Spinner `ogy wrap` TTY vs Pipe** : vérifier qu'en vrai PTY/ConPTY `stderr.isatty()` est `True` et affiche le spinner 1 ligne puis l'efface proprement, tandis qu'en pipe `stderr` ne contient **0 séquence ANSI**.
  2. **Délégation TTY `ogy run` & Coexistence Simultanée V3.1** :
     * Exécuter `ogy run` dans un vrai terminal face à un faux script `fake_agy_cli.py` dans un `HOME` temporaire initialisé en `ogy init --isolated`.
     * Vérifier que le faux `agy` reçoit `isatty() == True`, `OGY_TASK_ID`, `AGY_CLI_DISABLE_AUTO_UPDATE="true"`, `OGY_ACTIVE="1"`, ainsi que `--settings <isolated_settings.json>` et l'env du profil isolé.
     * Vérifier en parallèle qu'un appel direct au faux `agy` hors `ogy` ne voit ni `OGY_ACTIVE`, ni hooks `ogy`, et que le `~/.gemini/` temporaire reste **100 % vierge**.
     * Vérifier l'absence de processus enfant ou thread orphelin après sortie ou timeout sous Windows.
  3. **Lancement complet `ogy tui` dans un vrai terminal** : relié au Fake ACP Agent stdio, attente déterministe `wait_idle` / `get_by_text` (sans `sleep`), saisie d'une commande et sortie propre (`Ctrl+Q` / code de retour `0`).
* **Critères « Done »** : batterie courte (3–5 smoke tests) verte sur Linux, macOS et Windows dans `cross-os-matrix`.

---

### Lot 3.5.8 — Durcissement Hebdomadaire par Tests de Mutation (`mutmut v3`)
* **Fichiers** : `pyproject.toml` (`[tool.mutmut]`), `.github/workflows/mutation.yml` (nouveau, cron hebdomadaire)
* **Actions précises** :
  1. **Ciblage chirurgical (`[tool.mutmut]`)** :
     * `only_mutate` : `ogy_core/firebreaks.py`, `ogy_core/router.py`, `ogy_core/pricing.py`, `ogy_core/context_health.py` + **garde-fou V3.1** (`ogy_hooks/cli.py` et logique backup/restore/isolation V3.1 dans `ogy_cli/init_audit.py` ou `ogy_core/profile.py`).
     * `mutate_only_covered_lines = true`, `max_stack_depth = 8`.
  2. **Respect des 2 contraintes techniques de `mutmut v3.5`** :
     * **Linux / WSL uniquement** (requiert `fork`, ne tourne pas sous Windows natif → job CI `ubuntu-latest` uniquement).
     * **Mode non éditable obligatoire** : synchroniser l'env CI avec `uv sync --all-extras --no-editable` (contourne le bug `mutmut#486` des fichiers `.pth` en installation éditable).
* **Critères « Done »** : job hebdomadaire vert sur `ubuntu-latest`, score de mutation suivi sur les bornes critiques (tokens, sécurité, isolation V3.1).

---

## Récapitulatif de Séquencement V3.5

| Phase | Lots | Ajouts `[dev]` | Validations manuelles supprimées |
| :--- | :--- | :--- | :--- |
| **Phase 1 — Cœur TUI, ACP & V3.1** | `3.5.1` Fake ACP Agent stdio + Factories Pilot + `v31_env_audit` | *(aucune)* | Lancement manuel `agy acp`, consommation quotas OAuth, vérif env V3.1 |
| **Phase 2 — Visuel, Contrats & Temps** | `3.5.2` Snapshots SVG → `3.5.3` MCP stdio & Fake GenAI → `3.5.4` `time-machine` & CLI snapshots | `pytest-textual-snapshot`, `time-machine`, `inline-snapshot`, `dirty-equals`, `syrupy` | Inspections CSS manuelles, vérif formatage CLI/HUD/V3.1, dates/quotas, pannes stdio |
| **Phase 3 — Orchestration Zéro Effort** | `3.5.5` `testmon` + `Justfile` → `3.5.6` GitHub Actions (`diff-cover`, 3 OS) | `pytest-xdist`, `pytest-timeout`, `pytest-cov`, `diff-cover`, `pytest-testmon` | Choix manuel des tests en local, vérifications manuelles cross-OS avant merge |
| **Phase 4 — Terminal Réel & Mutation** | `3.5.7` E2E TTY (`tui-test`) + coexistence V3.1 → `3.5.8` `mutmut v3` hebdo | `tui-test` (ou `pywinpty`), `mutmut` (CI Linux seul) | Vérif PTY/ConPTY Windows/POSIX, coexistence `ogy`/`agy` réelle, chasse aux bornes |
