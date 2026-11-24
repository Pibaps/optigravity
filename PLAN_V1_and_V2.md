Voici le plan d’implémentation complet d’**optigravity (`ogy`)**, découpé en **Phase 0 (Validation locale)**, **Partie 1 (V1 : Socle Niveau 1 + Niveau 2)** et **Partie 2 (V2 : Client Interactif Niveau 4 sur `agy acp`)**.

Il consolide toutes les spécifications vérifiées, schémas exacts, formules économiques et pièges terrain issus des deux documents d’architecture ([Architecture Optigravity sur Google Antigravity.pdf](file:///c:/Users/pbapi/Documents/Projets/optigravity/Architecture%20Optigravity%20sur%20Google%20Antigravity.pdf) et [Architecture Optigravity pour Google Antigravity.pdf](file:///c:/Users/pbapi/Documents/Projets/optigravity/Architecture%20Optigravity%20pour%20Google%20Antigravity.pdf)) afin d’éviter toute régression, casse de cache, blocage de hook ou violation des conditions d’utilisation (ToS).

---

## Phase 0 — Prérequis : Sondes de validation locale (`ogy doctor --probe-agy`)

Avant d’activer les garde-fous (`hooks.json`) ou de coder le client ACP, implémenter et exécuter une commande `ogy doctor --probe-agy` (~10 min) pour valider sur **votre build locale d’`agy`** les comportements marqués `[À TESTER]` :

1. **Flags et slugs exacts** : exécuter `AGY_CLI_DISABLE_AUTO_UPDATE=true agy --version`, `agy --help` et `agy models`. Confirmer l’absence de `--settings`, la syntaxe de `--disable-telemetry` (vs `--disable_telemetry`) et le support de `AGY_CLI_AUTO_COMPACT_OVERRIDE`.
2. **Chemin actif MCP** : exécuter `agy mcp list` pour trancher l’ambiguïté entre `~/.gemini/config/mcp_config.json`, `~/.gemini/antigravity-cli/mcp_config.json` et `.agents/mcp_config.json`.
3. **Schéma local des logs et bases internes (lecture seule stricte)** :
   * Inspecter les clés de `transcript.jsonl` vs `transcript_full.jsonl` sous `~/.gemini/antigravity-cli/brain/<id>/.system_generated/logs/`.
   * Vérifier en lecture seule (`file:<path>?mode=ro&immutable=1`) la présence de `gen_metadata` dans `conversations/<id>.db` et des colonnes plates de `conversation_summaries.db`.
4. **Sonde `PreToolUse` critique** :
   * Tester si `overwrite: {"CommandLine": "..."}` est bien appliqué sous `--mode=auto` **et** sous `toolPermission=request-review` (bug historique `#575`).
   * Tester en interactif que `{"decision": ""}` (no-op depuis v1.0.16) laisse bien passer les demandes de confirmation natives (contrairement à `"allow"`, qui les contournait dans `#5078`).
   * Vérifier si `--dangerously-skip-permissions` désactive les hooks et si `.agents/hooks.json` s’exécute sous `GEMINI_API_KEY` (`#893`).
   * Capturer le format exact de `toolCall.name` (vérifier `view_file` vs `read_file` et le préfixage des outils MCP).
5. **Sonde `statusLine` et `agy acp`** :
   * Capturer un payload réel `statusLine` pour confirmer `cache_read_input_tokens`, `quota`, `cost`, `exceeds_200k_tokens`.
   * Envoyer `initialize` + `session/new` à `agy acp` pour vérifier les `configOptions` (`model`, `mode`, `thinking_level`/`thought_level`), la présence de `capabilities.session.fork` et les modèles exposés sous `clientInfo.name = "optigravity"`.
6. **Benchmark *cold-start* Python** : mesurer `python3 -S` (stdlib pure) sur 10 exécutions — cible p50 < 35 ms.

---

# PARTIE 1 — PLAN COMPLET V1 : Socle « Core-First » (Niveau 1 + Niveau 2)

**Objectif V1 :** Livrer **35 % à 55 % d’économie de tokens**, un HUD temps réel dans la TUI native `agy` et des garde-fous déterministes, tout en conservant **100 % de la TUI native**, **100 % des quotas OAuth Google AI Pro/Ultra (y compris Claude 5.5)** et **0 risque ToS**.

**Stack V1 :** Python 3.12+ packagé via `uv tool install optigravity`.
**Règle d’or de performance :** Tous les points d’entrée appelés en boucle par `agy` (`ogy-hook`, `ogy statusline`) n’importent **que la bibliothèque standard** (`sys`, `json`, `sqlite3`, `hashlib`, `os`, `tomllib`, `pathlib`) — **zéro** `pydantic`, `rich`, `textual` ou `numpy` dans ce chemin chaud.

### 1.1 Structure modulaire du dépôt

```text
optigravity/
├── pyproject.toml                  # Points d'entrée: ogy, ogy-hook, ogy-mcp
├── ogy_core/
│   ├── config.py                   # Lecture ~/.config/optigravity/config.toml (indépendant des env vars)
│   ├── storage.py                  # Couche SQLite WAL, migrations, vues KPI, 8 requêtes waste
│   ├── ingest.py                   # Collecteur hybride (statusLine + transcript_full + *.db RO)
│   ├── pricing.py                  # Catalogue tarifaire versionné (dont bascule 1er déc. 2026)
│   ├── router.py                   # Routeur Étage 1 (heuristique) + Étage 2 (bandit SQLite)
│   ├── context_health.py           # Zones verte/ambre/rouge, formule K*, détection cache froid
│   └── firebreaks.py               # Règles view_file/read_file, ogy wrap, anti-boucle, stash
├── ogy_hooks/
│   └── cli.py                      # Dispatcher fail-open stdlib-only: ogy-hook <event>
├── ogy_statusline/
│   └── render.py                   # Rendu ANSI 1-2 lignes (< 20-40 ms) + snapshot debounced
├── ogy_mcp/
│   └── server.py                   # Serveur MCP stdio (mcp>=2,<3, 3 à 5 outils concis)
├── ogy_cli/
│   ├── main.py                     # CLI: run, init, stats, waste, audit, doctor, recall, wrap
│   └── wrapper.py                  # Lanceur Niveau 1 sans proxy PTY
└── templates/
    ├── ogy-hygiene.md              # Règle always_on (< 200 tokens)
    └── ogy-frugal-engineering/SKILL.md  # Skill manuel /ogy-budget + handoff
```

---

### 1.2 Module 1 — Persistance SQLite WAL (`ogy_core/storage.py`)

**Emplacement :** `~/.local/share/optigravity/ogy.db` (surchargeable via `OGY_DB_PATH`).

**Initialisation obligatoire sur CHAQUE connexion :**
```sql
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;
PRAGMA auto_vacuum = INCREMENTAL;
PRAGMA journal_size_limit = 67108864; -- 64 MiB
PRAGMA temp_store = MEMORY;
```

**Invariants anti-verrou et anti-fragmentation :**
* Toute écriture commence obligatoirement par `BEGIN IMMEDIATE` (évite les deadlocks d’escalade `BEGIN DEFERRED` qui renvoient `SQLITE_BUSY` sans honorer `busy_timeout`).
* Tout `SELECT` consomme immédiatement tous ses résultats (`fetchall()`) et ferme la transaction (prévient la *checkpoint starvation* où le fichier `-wal` grossit sans fin).
* Dans le hook `Stop` uniquement : exécuter `PRAGMA wal_checkpoint(PASSIVE);` et `PRAGMA incremental_vacuum(200);`.

**Schéma relationnel (7 tables) :**
1. `tasks` : `task_id` (PK), `repo_root`, `git_branch`, `git_head_start`, `git_head_end`, `title`, `category` (`trivial_edit`, `lookup_qa`, `routine_code`, `bugfix_local`, `bugfix_complex`, `refactor_multifile`, `architecture_security`, `research_review`), `tags_json`, `outcome` (`in_progress`, `success`, `partial`, `failed`, `rolled_back`, `aborted`), `verifier_passed` (1/0/NULL), `rollback_count`, `created_at`, `completed_at`.
2. `sessions` : `session_id` (PK = UUID `conversation_id`), `parent_session_id` (FK self), `root_session_id`, `nesting_depth` (0..10), `agent_name`, `subagent_role`, `task_id` (FK), `surface` (`cli_tui`, `cli_headless`, `cli_acp`, `sdk_python`, `app`), `workspace_path`, `agy_version`, `plan_tier`, `initial_prompt`, `initial_model`, `initial_effort`, `initial_mode`, `sandbox_enabled`, `transcript_path`, `transcript_byte_offset`, `last_ingested_step`, `status`, `compaction_count`, `total_cost_usd`, `subagent_cost_usd`, `started_at`, `ended_at`, `wall_duration_ms`.
3. `turns` : `turn_id` (PK), `session_id` (FK), `task_id` (FK), `step_index`, `invocation_num`, `response_id` (index `UNIQUE WHERE response_id IS NOT NULL` pour dédupliquer forks/swarms), `model_id`, `effort_level`, `execution_mode`, `input_tokens`, `cache_read_tokens`, `cache_creation_tokens`, `output_tokens`, `thinking_tokens`, `thinking_is_estimated` (0/1), `context_window_used`, `context_window_max`, `exceeds_200k`, `system_prompt_tokens`, `tools_schema_tokens`, `ttft_ms`, `duration_ms`, `gap_since_prev_turn_s`, `estimated_cost_usd`, `is_compaction_turn`, `data_source` (`statusline`, `stream_json`, `acp`, `conversation_db`, `transcript_est`, `sdk`), `created_at`. Contrainte `UNIQUE(session_id, step_index)` pour UPSERT idempotent.
4. `tool_calls` : `tool_call_id` (PK), `session_id` (FK), `turn_id` (FK), `step_index`, `tool_name`, `tool_category` (`shell`, `file_read`, `file_write`, `search`, `mcp`, `web`, `subagent`, `other`), `mcp_server`, `target_path_or_cmd`, `args_hash` (SHA-256 canonique), `args_bytes`, `raw_output_bytes`, `context_output_bytes`, `est_output_tokens`, `was_truncated_by_agy`, `hook_action` (`none`, `allow`, `overwrite`, `deny`, `warn`, `compressed`), `firebreak_rule`, `exit_code`, `is_error`, `error_message`, `duration_ms`, `created_at`. Contrainte `UNIQUE(session_id, step_index, tool_name, args_hash)`.
5. `routing_decisions` : `decision_id` (PK), `session_id`, `task_id`, `stage` (`stage1_heuristic`, `stage2_bandit`, `escalation`, `manual_override`), `detected_category`, `features_json`, `chosen_model`, `chosen_effort`, `chosen_mode`, `propensity`, `predicted_score`, `actual_reward`, `escalated_from_id`, `created_at`.
6. `quota_snapshots` : `snapshot_id` (PK), `session_id`, `bucket_id` (`gemini-5h`, `gemini-weekly`, `claude-5h`, `claude-weekly`), `remaining_fraction`, `reset_time`, `reset_in_seconds`, `plan_tier`, `captured_at`.
7. `hook_events` : `event_id` (PK), `session_id`, `hook_type`, `action_taken`, `rule_name`, `tokens_saved_est`, `hook_latency_ms`, `created_at`.

**Vues KPI & 8 requêtes `ogy waste` :**
* `v_session_kpis` : agrège par session avec formule de cache hit unifiée `cache_read_tokens / NULLIF(input_tokens + cache_read_tokens + cache_creation_tokens, 0)` (juste à la fois pour Gemini implicite où `cache_creation = 0` et pour Claude 5.5 explicite).
* `v_task_rollup_with_subagents` : `WITH RECURSIVE` jusqu’à profondeur 10 attribuant les tokens/coûts de tous les sous-agents à leur session racine.
* `v_routing_performance` : taux de succès, coût moyen et tours moyens par `(category, model_id, effort_level)`.
* **Les 8 requêtes automatiques de détection du gaspillage (`ogy waste`)** :
  1. Cache hit faible (< 60–80 %) avec distinction *wipe serveur* (`gap < 180s`) vs *expiration TTL* (`gap >= 180s`).
  2. Boucles de retries (`COUNT(*) >= 3` et `SUM(is_error) >= 2` sur même `args_hash`).
  3. Sorties d’outils obèses non filtrées (> 4 000–8 000 tokens ou ≥ 32 Ko en un appel).
  4. Sur-effort de raisonnement (`high`/`xhigh`/`max` sur `trivial_edit`/`lookup_qa`, ou ratio `thinking / output > 5–8×`).
  5. Sessions trop longues exposées au *context rot* (pic ≥ 120K–175K tokens, ≥ 25–35 tours ou rollbacks > 0).
  6. Relectures dupliquées d’un même fichier (`view_file`/`read_file` ≥ 2× sans compaction).
  7. Inflation des schémas d’outils MCP (> 15K tokens rejoués par tour).
  8. Dérive des sous-agents (`subagent_cost > parent_cost` ou sous-agents en échec).

---

### 1.3 Module 2 — Ingestion hybride multi-sources (`ogy_core/ingest.py`)

Hiérarchiser les sources pour éviter le double-comptage et combler les manques de chaque format :

1. **Temps réel (`statusLine` JSON)** : source primaire pour `input_tokens`, `cache_read_tokens`, `cache_creation_tokens`, `output_tokens`, `cost.{total_usd, subagent_usd}`, `quota`, `context_window` et `exceeds_200k_tokens`. *(Limite : `thinking_tokens` absent).*
2. **Incrémental disque (`transcript_full.jsonl`)** : lecture à partir de `transcript_byte_offset` / `last_ingested_step` (déclenchée en `PostInvocation` et `Stop`).
   * Toujours lire `transcript_full.jsonl` (et fallback `transcript.jsonl` + `chunks/`) pour obtenir `raw_output_bytes` réel sans troncature.
   * Nettoyer l’enveloppe `<USER_REQUEST>` sur les prompts.
   * Faire un `UPSERT` sur `(session_id, step_index)` ne conservant que l’état final (`DONE`) ou maximal (évite les bugs `first-seen` de `ccusage`).
3. **Enrichissement post-tour / détaché (lecture seule stricte)** :
   * Ouvrir `~/.gemini/antigravity-cli/conversations/<id>.db` **uniquement** avec `file:<path>?mode=ro&immutable=1` + `PRAGMA query_only = ON` (jamais sur le chemin critique temps réel). Décoder `gen_metadata` pour extraire `usage.9` (`thinking_tokens` exact), `ResponseID`, `d11` (TTFT) et `ContextBreakdown` (poids exact `System Prompt` / `Tools`). En cas de changement de schéma protobuf, basculer silencieusement sur `thinking_is_estimated = 1`.
   * Lire `conversation_summaries.db` (SQL plat, zéro protobuf) pour relier `parent_session_id` ↔ subagents, en résolvant le chemin `brain/` via la colonne `app_data_dir` (car l’App Desktop y miroite les sessions CLI).

---

### 1.4 Module 3 — Lanceur CLI Niveau 1 (`ogy_cli/wrapper.py` & `main.py`)

* **`ogy run [options] [prompt]`** :
  1. Exécute le Routeur Étage 1 (< 5 ms) sur le prompt + état Git (`git status --porcelain`, `git diff --shortstat`).
  2. Prépare les variables d’environnement : `AGY_CLI_DISABLE_AUTO_UPDATE=true` (chaîne exacte `"true"`, jamais `"1"`), `OGY_TASK_ID`, `OGY_SESSION_META`.
  3. Construit les flags officiels : `--model <slug>`, `--effort <level>` (omis si `Orchestrator`), `--mode <mode>`, `--sandbox` si requis. *(Ne jamais passer `--settings`, `--permission-mode` ni `-m`, inexistants dans `agy`).*
  4. **Transfert direct du TTY (zéro proxy PTY)** : `os.execvp("agy", args)` sous POSIX ; sous Windows, `subprocess.run` avec héritage direct de `stdin/stdout/stderr` et relais transparent de `SIGINT`. Préserve 100 % du mode *alt-screen*, `SIGWINCH`, Vim et `Ctrl+V` images.
* **Commandes compagnon** :
  * `ogy init --dry-run` / `ogy init` : installe de façon réversible `statusLine` dans `settings.json`, `hooks.json` dans `~/.gemini/config/hooks.json`, la règle `ogy-hygiene.md`, le skill et l’entrée serveur MCP.
  * `ogy audit mcp` : détecte les outils `eager: true`, les serveurs hors-projet et les outils UI (`_meta.ui.visibility: ["app"]`) qui fuient dans le contexte CLI, puis génère un profil `enabledTools` minimal par dépôt.
  * `ogy stats`, `ogy waste`, `ogy recall <hash> [--lines A:B]`, `ogy wrap -- <cmd>`, `ogy doctor`.

---

### 1.5 Module 4 — Routeur 2 étages & Politique de Contexte (`ogy_core/router.py`, `context_health.py`)

**Règle cardinale de *stickiness* :** Le routeur décide **au lancement** (ou à la frontière d’un sous-agent). Il ne change jamais de modèle/effort en milieu de session chaude, car l’invalidation du préfixe impose un point mort de :
$$\Delta_{\text{think}} \ge (m_w - m_r) \cdot \frac{p_{\text{in}}}{p_{\text{out}}} \cdot C_{\text{prefix}}$$
*(Ex. sur Opus 5.5 à 100K de préfixe : baisser l’effort coûte \$0,48 et exige d’économiser ≥ 24 000 tokens de thinking).*

**Étage 1 — Classifieur heuristique déterministe (< 5 ms, actif Jour 1) :**
Vecteur normalisé de 8 features : longueur du prompt, fichiers `@`-mentionnés, taille du repo, présence d’un test runner (`pytest`/`vitest`/`cargo`/`go`), portée multi-fichiers, présence de stacktrace, mots-clés architecture/sécurité, urgence/exploration + pression quota (`remaining_fraction < 0.15` → rétrogradation automatique d’un cran d’effort).

| Catégorie | Modèle par défaut | Effort | Mode | Garde-fous & Escalade |
| :--- | :--- | :--- | :--- | :--- |
| `trivial_edit` | `gemini-4-hydrogen` | `low` | `accept-edits` | Escalade → `medium` si lint/test KO |
| `lookup_qa` | `gemini-4-hydrogen` | `low` | `plan` | Privilégier `/btw` en session active |
| `routine_code` | `gemini-4-hydrogen` (ou `sonnet-5.5`) | `medium` | `auto` | Si vérificateur auto : `medium` → `neon high` sur échec |
| `bugfix_local` | `gemini-4-neon` | `low`→`medium` | `auto` | Cascade vérifiée : 2 échecs → `neon high` |
| `bugfix_complex` | `gemini-4-neon` | `high` | `auto` | Plancher `neon medium` ; jamais `carbon low` (4,7× hallucinations) |
| `refactor_multifile` | `gemini-4-neon` | `medium` | `plan`→`auto` | Éviter `Orchestrator` sur boucles > 5 min (taxe 2,58× input non caché) |
| `architecture_security` | `gemini-4-neon` / `carbon medium` / `opus-5.5` | `high` | `plan` | Plancher dur ; **`max` banni en automatique** (`xhigh` explicite seul) |

**Étage 2 — Bandit contextuel local SQLite (actif après 15 à 50 tâches qualifiées) :**
* Maintient par couple `(catégorie, modèle × effort)` une moyenne pondérée exponentiellement (facteur d’oubli $\eta = 0,92$ pour absorber les mises à jour silencieuses des modèles) + sélection `LinUCB` / `UCB1` régulée par la pression de quota.
* Récompense calculée dans le hook `Stop` : combine succès vérifié ($1,0$ si tests OK sans rollback, $0,4$–$0,5$ sans vérificateur, $0,0$ si échec/rollback), coût normalisé et pénalité de retries/escalade.
* **Garde-fou qualité :** tout bras dont le taux de succès tombe sous **80 %** sur une catégorie est automatiquement exclu ; tout flag explicite `--model`/`--effort` l’emporte et est tracé comme `manual_override`.

**Politique de santé de contexte (`context_health.py`) :**
* **Zone verte** (`< 60K` tokens, `< 15` tours, cache hit ≥ 60 %) : garder chaud, ne rien toucher.
* **Zone ambre** (`60K–100K` tokens ou `15–25` tours) : mode frugal strict, `/btw` pour questions annexes, préparer la sortie de sous-tâche.
* **Zone rouge** (`≥ 100K–120K` tokens, ou `exceeds_200k_tokens = true`, ou `idle_gap > 180–300 s` sur session `> 60K–80K`, ou `≥ 2` échecs consécutifs) :
  * *Défaut* : écrire `.ogy/handoff.md` (≤ 2,5K tokens : objectif, fichiers modifiés, vérifications, contraintes négatives, prochaine étape) puis `/clear` (> 50 % moins cher que `/compact` et zéro *context rot*).
  * *Exception* (même tâche insécable avec ≥ $K^*$ tours restants : ≥ 5 sur Gemini 4, ≥ 9 sur Claude) : `/compact Conserve : <fichiers>, <contraintes négatives>, <prochaine étape>`.
  * *Jamais compacter après 2 échecs répétés* (la résumation lisse les signaux d’erreur) → exiger `/rewind` ou `/clear`.
* **Falaise tarifaire dure** : alerte rouge à `> 90K` sur Claude Haiku 5.5 (saut tarifaire 5× à 100K) et à l’approche des `200K` sur Gemini long-context.

---

### 1.6 Module 5 — HUD temps réel `statusLine` (`ogy_statusline/render.py`)

**Configuration dans `~/.gemini/antigravity-cli/settings.json` :**
```json
{
  "statusLine": {
    "type": "command",
    "command": "ogy-hook statusline",
    "enabled": true,
    "padding": 0,
    "stack_with_default": true
  }
}
```
*(Pièges Windows : pas de `~` ni d’antislashs simples dans `command` ; utiliser le chemin absolu avec `/` ou le binaire sur le `PATH`).*

**Contrat d’exécution :**
* Reçoit le JSON sur `stdin` (au max 1× / 300 ms).
* Calcule : `cache_hit_pct`, `idle_gap_s` (alerte pré-éviction dès 150 s sur Gemini 4, `[CACHE FROID → /ogy-handoff]` dès 300 s), zone de santé, coût et quotas `5h / 7j`.
* Persiste un snapshot SQLite *debounced* (≤ 1 écriture / 10 s, timeout SQLite interne < 500 ms).
* Rend 1 ligne ANSI compacte adaptée à `terminal_width` (respecte `NO_COLOR`, mode ultra-compact si largeur < 120 col.) :
  `ogy | cache 91% | ctx 64K (12%) | $0.24 | 5h:88% | OK`
* **Tolérance aux pannes : *fail-open* absolu** (`try/except BaseException`, affichage minimal depuis `stdin` seul, `exit(0)`) pour ne jamais approcher les 30 échecs consécutifs qui désactivent définitivement `statusLine`.

---

### 1.7 Module 6 — Hooks & Moteur de Firebreaks (`ogy_hooks/cli.py`, `ogy_core/firebreaks.py`)

**Configuration dans `~/.gemini/config/hooks.json` (JSON strict, zéro commentaire `//`, timeouts en secondes, jamais dans `settings.json`) :**
* Tableaux plats pour `SessionStart`, `PreInvocation`, `PostInvocation`, `Stop`.
* Objets `{matcher, hooks}` pour `PreToolUse` (`matcher: "run_command|view_file|read_file"`) et `PostToolUse` (`matcher: "*"`).
* L’événement est passé en argument CLI (`ogy-hook pre-tool`, etc.) car `hookEventName` est absent de `stdin`.
* Le bloc `ogy-firebreaks` (`PreToolUse`) est livré avec `"enabled": false` tant que `ogy doctor --probe-agy` n’a pas validé localement `overwrite` et `decision: ""`.

**Comportement exact par événement :**
1. **`SessionStart`** : crée l’entrée `sessions` dans `ogy.db`. Si un `.ogy/handoff.md` en attente existe, l’injecte **une seule fois** via `injectSteps` (préfixe 100 % cachable) puis archive le fichier.
2. **`PreToolUse`** : **ne jamais renvoyer `{}`** (bloque tous les outils) ; utiliser `{"decision": ""}` (no-op sûr depuis v1.0.16) en nominal :
   * *Firebreak 1 (Anti-boucle)* : si `args_hash` a déjà échoué ≥ 2–3 fois dans la session → `{"decision": "deny", "reason": "[ogy] Boucle d'erreur détectée, change d'approche."}`.
   * *Firebreak 2 (Lecture obèse)* : si `view_file`/`read_file` cible un lockfile (`package-lock.json`, `Cargo.lock`, `pnpm-lock.yaml`) ou un fichier > 16 Ko (~300–350 lignes) sans `StartLine`/`EndLine` → `deny` guidant vers `grep_search`, une plage bornée ou `ogy_code_skeleton`.
   * *Firebreak 3 (Compression shell `ogy wrap`)* : sur `run_command` verbeux (`pytest`, `cargo test`, `npm test`, `git log`, `git diff`, etc.), **en excluant strictement** les commandes mutantes/cryptographiques (`git commit`, `git push`, `git rebase`, `rm`) :
     * Si `overwrite_verified_ok = true` : renvoie `{"decision": "", "overwrite": {"CommandLine": "ogy wrap -- <cmd>"}}`.
     * Sinon : repli `deny` + suggestion explicite de la commande wrappée.
   * **`ogy wrap`** : filtre déterministe + *net-win gate* (laisse passer brut si pas de gain) + *tee-on-failure* sauvegardant la sortie brute dans `~/.cache/optigravity/stash/<hash>.log` (récupérable par `ogy_recall`) + préservation garantie du code de sortie.
3. **`PostToolUse`** : renvoie strictement `{}` (ne reçoit pas la sortie d’outil). Enregistre `tool_calls` (`args_hash`, `error`) et incrémente le compteur anti-boucle.
4. **`PreInvocation`** : **muet (`{}`)** en régime nominal et dès que `invocationNum > 1` (pour ne jamais casser le Prompt Cache). Intervient uniquement sur dépassement d’un plafond budgétaire dur (`terminationBehavior: "terminate"` + `errorMessage`) ou alerte unique de palier (suivie via `/tmp/ogy-state/<id>.json`).
5. **`PostInvocation`** : lit incrémentalement `transcript_full.jsonl` depuis `transcript_byte_offset`, renvoie `{}`.
6. **`Stop`** : lance l’ingestion finale détachée (y compris `conversations/<id>.db` RO), calcule `routing_decisions.actual_reward`, exécute `wal_checkpoint(PASSIVE)` + `incremental_vacuum(200)`, renvoie `{"decision": "stop"}` (ou `{}`).

**Invariant absolu :** Tout `ogy-hook` encapsule son `main()` dans `try/except BaseException` → JSON neutre valide + `sys.exit(0)` sous 2 s (budget timeout `hooks.json` = 5 s). Lit sa config depuis `~/.config/optigravity/config.toml` (car `agy` sanitise l’environnement des hooks).

---

### 1.8 Module 7 — Serveur MCP minimal & Règles/Skills sobres

* **Serveur MCP (`ogy_mcp/server.py`)** : basé sur le SDK Python `mcp>=2,<3` (`MCPServer`, handlers synchrones sur thread pool, **zéro `print()` sur `stdout`**). Configuré en `eager: false` (*lazy loading* natif). Expose 3 outils cœur (< 450 tokens) + 2 outils AST optionnels (< 1 500 tokens cumulés) :
  1. `ogy_session_budget` : retourne `active_context_tokens`, `warm_cache_hit_pct`, `idle_gap_s`, top gaspillage et conseil (`/clear + handoff`, `/compact`, ou continuer).
  2. `ogy_recall` : lecture bornée (par hash + plage de lignes) d’une sortie brute sauvegardée par `ogy wrap`.
  3. `ogy_write_handoff` : génère un `.ogy/handoff.md` normalisé (≤ 30 lignes / 2,5K tokens).
  4. *(Optionnel P1)* `ogy_code_skeleton` & `ogy_symbol_slice` (via `tree-sitter`) : signatures avec ancres de lignes (`[L45-88: 43 lines omitted]`).
* **Règle permanente (`~/.gemini/config/rules/ogy-hygiene.md`, < 200 tokens)** : frontmatter `trigger: always_on` (impérativement `snake_case`, jamais `alwaysOn`). 5 consignes denses : localiser avant de lire, ne pas relire sans mutation, borner les commandes shell, grouper les appels indépendants, répondre sans remplissage.
* **Skill manuel (`~/.gemini/config/skills/ogy-budget/SKILL.md`)** : `disable-model-invocation: true` (coût 0 token dans le prompt système hors invocation), procédure en 3 étapes + tableau anti-rationalisation.

### 1.9 Jalons & Critères d’acceptation V1

* **MVP (Semaines 1–2) :** `storage.py` + `ingest.py` + `ogy run` (Étage 1) + `ogy statusline` + `ogy stats`/`waste`/`doctor`.
  * *Acceptation :* latence `statusline` p95 < 25–40 ms ; 0 erreur `SQLITE_BUSY` ; 100 % des subagents rattachés à leur `root_session_id` ; 0 double-comptage (UPSERT vérifié).
* **V1 complète (Semaines 3–5) :** `hooks.json` fail-open + `ogy wrap` + serveur MCP minimal + règle/skill + `ogy audit mcp` + Routeur Étage 2.
  * *Acceptation (mesurée sur 20 tâches réelles avant/après) :* baisse **≥ 35 % à 55 %** des tokens nets facturables par tâche réussie ; cache hit rate chaud **≥ 80–88 %** sur Gemini 4 et **≥ 93 %** sur Claude 5.5 ; réduction **≥ 65 %** des octets shell ; 0 blocage dû à un crash hook ; 0 régression sur les tests.

---

### 1.10 Annexe d'implémentation V1 (référence autonome, sans relecture des PDFs)

#### A. Packaging (`pyproject.toml`) et contraintes OS (Windows / POSIX)
* **Dépendances runtime V1 :** `mcp>=2,<3` (uniquement importé par `ogy_mcp/server.py` ; `ogy_hooks/cli.py` et `ogy_statusline/render.py` restent **100 % stdlib**). Dépendance dev : `pytest`.
* **Points d'entrée :** `ogy = "ogy_cli.main:main"`, `ogy-hook = "ogy_hooks.cli:main"`, `ogy-mcp = "ogy_mcp.server:main"`.
* **Spécificités Windows :**
  * `ogy run` utilise `subprocess.run(["agy", *args])` (héritage direct des descripteurs `stdin/stdout/stderr`) au lieu de `os.execvp`.
  * `statusLine` est exécuté via `cmd /c` sous Windows : ne jamais mettre `~` ni d'antislashs simples dans `settings.json` (utiliser `ogy-hook statusline` si sur le `PATH`, ou un chemin absolu avec `/`).
  * Chemins Antigravity : `Path.home() / ".gemini" / "antigravity-cli"` (pour `settings.json`, `conversations/*.db`, `brain/`, `conversation_summaries.db`) et `Path.home() / ".gemini" / "config"` (pour `hooks.json`, `rules/`, `skills/`).

#### B. Grille tarifaire initiale (`ogy_core/pricing.py`, USD / 1M tokens)
* **Gemini 4 Neon :** input `$3.00`, output/thinking `$15.00`, cache read `0.05×` (API) / `0.10×` (Enterprise), cache write `1.0×`.
* **Gemini 4 Hydrogen :** input `$1.50`, output/thinking `$7.50`, cache read `0.05×`/`0.10×`, cache write `1.0×`.
* **Gemini 4 Carbon / Argon :** input `$2.00` (puis `$4.00` à partir du `2026-12-01`), output/thinking `$10.00` (puis `$20.00` au `2026-12-01`), cache read `0.05×`/`0.10×`, cache write `1.0×`.
* **Claude Opus 5.5 :** input `$4.00`, output/thinking `$20.00`, cache write 5m `1.25×` (1h `2.0×`), cache read `0.05×`.
* **Claude Sonnet 5.5 :** lecture cache `0.05×`, écriture 5m `1.25×`. *(Tarif exact configurable dans `pricing.py` / `config.toml`).*

#### C. Squelette exact de `~/.gemini/config/hooks.json`
```json
{
  "ogy-telemetry-and-budget": {
    "enabled": true,
    "SessionStart": [
      { "type": "command", "command": "ogy-hook session-start", "timeout": 5 }
    ],
    "PreInvocation": [
      { "type": "command", "command": "ogy-hook pre-invocation", "timeout": 3 }
    ],
    "PostToolUse": [
      {
        "matcher": "*",
        "hooks": [{ "type": "command", "command": "ogy-hook post-tool", "timeout": 5 }]
      }
    ],
    "PostInvocation": [
      { "type": "command", "command": "ogy-hook post-invocation", "timeout": 5 }
    ],
    "Stop": [
      { "type": "command", "command": "ogy-hook stop", "timeout": 10 }
    ]
  },
  "ogy-firebreaks": {
    "enabled": false,
    "PreToolUse": [
      {
        "matcher": "run_command|view_file|read_file",
        "hooks": [{ "type": "command", "command": "ogy-hook pre-tool", "timeout": 5 }]
      }
    ]
  }
}
```
*Champs communs reçus sur `stdin` pour chaque hook :* `conversationId`, `workspacePaths`, `transcriptPath`, `artifactDirectoryPath`, `modelName`, `lastUserInput`.
*Spécificités `stdin` → `stdout` :*
* `PreToolUse` (`toolCall.{name, args}`, `stepIdx`) → nominal `{"decision": ""}` ; blocage `{"decision": "deny", "reason": "[ogy] ..."}` ; réécriture `{"decision": "", "overwrite": {"CommandLine": "ogy wrap -- ..."}}`.
* `PostToolUse` (`toolCall.{name, args}`, `stepIdx`, `error`) → strictement `{}`.
* `PreInvocation` (`invocationNum`, `initialNumSteps`) → nominal `{}` ; coupure dure `{"terminationBehavior": "terminate", "injectSteps": [{"errorMessage": {"userMessage": "[ogy] ..."}}]}`.
* `Stop` (`terminationReason`, `fullyIdle`, `error`) → `{"decision": "stop"}` (ou `{}`).

#### D. Schéma SQLite exact (`ogy_core/storage.py`)
```sql
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;
PRAGMA auto_vacuum = INCREMENTAL;
PRAGMA journal_size_limit = 67108864;
PRAGMA temp_store = MEMORY;

CREATE TABLE IF NOT EXISTS tasks (
  task_id TEXT PRIMARY KEY,
  repo_root TEXT NOT NULL,
  git_branch TEXT,
  git_head_start TEXT,
  git_head_end TEXT,
  title TEXT,
  category TEXT NOT NULL,
  tags_json TEXT NOT NULL DEFAULT '[]',
  outcome TEXT NOT NULL DEFAULT 'in_progress',
  verifier_passed INTEGER,
  rollback_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  completed_at TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
  session_id TEXT PRIMARY KEY,
  parent_session_id TEXT REFERENCES sessions(session_id) ON DELETE SET NULL,
  root_session_id TEXT NOT NULL,
  nesting_depth INTEGER NOT NULL DEFAULT 0,
  agent_name TEXT NOT NULL DEFAULT 'main',
  subagent_role TEXT,
  task_id TEXT REFERENCES tasks(task_id) ON DELETE SET NULL,
  surface TEXT NOT NULL DEFAULT 'cli_tui',
  workspace_path TEXT NOT NULL,
  agy_version TEXT,
  plan_tier TEXT,
  initial_prompt TEXT,
  initial_model TEXT,
  initial_effort TEXT,
  initial_mode TEXT,
  sandbox_enabled INTEGER NOT NULL DEFAULT 0,
  transcript_path TEXT,
  transcript_byte_offset INTEGER NOT NULL DEFAULT 0,
  last_ingested_step INTEGER NOT NULL DEFAULT -1,
  status TEXT NOT NULL DEFAULT 'active',
  compaction_count INTEGER NOT NULL DEFAULT 0,
  total_cost_usd REAL NOT NULL DEFAULT 0.0,
  subagent_cost_usd REAL NOT NULL DEFAULT 0.0,
  started_at TEXT NOT NULL,
  ended_at TEXT,
  wall_duration_ms INTEGER
);

CREATE TABLE IF NOT EXISTS turns (
  turn_id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
  task_id TEXT REFERENCES tasks(task_id) ON DELETE SET NULL,
  step_index INTEGER NOT NULL,
  invocation_num INTEGER,
  response_id TEXT,
  model_id TEXT NOT NULL,
  effort_level TEXT,
  execution_mode TEXT,
  input_tokens INTEGER NOT NULL DEFAULT 0,
  cache_read_tokens INTEGER NOT NULL DEFAULT 0,
  cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  thinking_tokens INTEGER NOT NULL DEFAULT 0,
  thinking_is_estimated INTEGER NOT NULL DEFAULT 0,
  context_window_used INTEGER,
  context_window_max INTEGER,
  exceeds_200k INTEGER NOT NULL DEFAULT 0,
  system_prompt_tokens INTEGER,
  tools_schema_tokens INTEGER,
  ttft_ms INTEGER,
  duration_ms INTEGER,
  gap_since_prev_turn_s REAL,
  estimated_cost_usd REAL NOT NULL DEFAULT 0.0,
  is_compaction_turn INTEGER NOT NULL DEFAULT 0,
  data_source TEXT NOT NULL DEFAULT 'statusline',
  created_at TEXT NOT NULL,
  UNIQUE(session_id, step_index)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_turns_response_id ON turns(response_id) WHERE response_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS tool_calls (
  tool_call_id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
  turn_id INTEGER REFERENCES turns(turn_id) ON DELETE SET NULL,
  step_index INTEGER NOT NULL,
  tool_name TEXT NOT NULL,
  tool_category TEXT NOT NULL DEFAULT 'other',
  mcp_server TEXT,
  target_path_or_cmd TEXT,
  args_hash TEXT NOT NULL,
  args_bytes INTEGER NOT NULL DEFAULT 0,
  raw_output_bytes INTEGER NOT NULL DEFAULT 0,
  context_output_bytes INTEGER NOT NULL DEFAULT 0,
  est_output_tokens INTEGER NOT NULL DEFAULT 0,
  was_truncated_by_agy INTEGER NOT NULL DEFAULT 0,
  hook_action TEXT NOT NULL DEFAULT 'none',
  firebreak_rule TEXT,
  exit_code INTEGER,
  is_error INTEGER NOT NULL DEFAULT 0,
  error_message TEXT,
  duration_ms INTEGER,
  created_at TEXT NOT NULL,
  UNIQUE(session_id, step_index, tool_name, args_hash)
);

CREATE TABLE IF NOT EXISTS routing_decisions (
  decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
  task_id TEXT REFERENCES tasks(task_id) ON DELETE SET NULL,
  stage TEXT NOT NULL,
  detected_category TEXT NOT NULL,
  features_json TEXT NOT NULL,
  chosen_model TEXT NOT NULL,
  chosen_effort TEXT NOT NULL,
  chosen_mode TEXT NOT NULL,
  propensity REAL NOT NULL DEFAULT 1.0,
  predicted_score REAL,
  actual_reward REAL,
  escalated_from_id INTEGER REFERENCES routing_decisions(decision_id),
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS quota_snapshots (
  snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT REFERENCES sessions(session_id) ON DELETE SET NULL,
  bucket_id TEXT NOT NULL,
  remaining_fraction REAL,
  reset_time TEXT,
  reset_in_seconds INTEGER,
  plan_tier TEXT,
  captured_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS hook_events (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  hook_type TEXT NOT NULL,
  action_taken TEXT,
  rule_name TEXT,
  tokens_saved_est INTEGER NOT NULL DEFAULT 0,
  hook_latency_ms REAL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_sessions_root ON sessions(root_session_id);
CREATE INDEX IF NOT EXISTS idx_turns_session_time ON turns(session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_tool_calls_hash ON tool_calls(session_id, tool_name, args_hash);
CREATE INDEX IF NOT EXISTS idx_quota_bucket_time ON quota_snapshots(bucket_id, captured_at);
```

---

# PARTIE 2 — PLAN COMPLET V2 : Client Interactif Niveau 4 (`agy acp`) & Chemin Chaud Natif

**Objectif V2 :** Lever les limites dures du Niveau 2 (changement dynamique de modèle/effort en session, rotation transparente multi-sessions, *steering*, affichage des `thoughtTokens` en direct) via une TUI sur mesure (`ogy tui` / `ogy chat`), tout en **réutilisant 100 % d’`ogy_core`** et en restant conforme aux **ToS Section 6**.

### 2.1 Garde-fous contractuels (ToS Section 6) & périmètre modèles

* **Client fin strict sur `oauth-personal`** :
  * L’humain saisit directement ses prompts bruts (zéro injection de prompt système caché, zéro définition d’outil sérialisée côté client, zéro boucle autonome sans validation humaine).
  * `clientInfo.name` est déclaré honnêtement (`"optigravity"`), sans jamais usurper l’identité d’un IDE partenaire (Zed, JetBrains, Xcode, Android Studio).
  * Toute bascule de modèle/effort ou rotation de session en mode OAuth personnel fonctionne par **suggestion affichée dans la TUI et validée en une touche par l’utilisateur** (l’automatisation complète étant réservée à `GEMINI_API_KEY` / Vertex AI).
* **Routage Claude 5.5 vs Gemini** : la doc ACP cantonnant en principe les modèles tiers aux IDE partenaires sous OAuth personnel, `ogy tui` (V2) opère par défaut sur la gamme **Gemini 4** du forfait ; si l’utilisateur demande explicitement Claude 5.5 sous forfait Pro/Ultra, `ogy` propose un basculement propre vers `ogy run` (V1).

---

### 2.2 Module 1 — Client JSON-RPC 2.0 ACP v1 (`ogy_frontends/acp_tui/client.py`)

Basé sur le SDK Python officiel `agent-client-protocol` (Pydantic + `asyncio`), lançant `agy acp` en sous-processus sur `stdio` avec les flags `--sandbox`, `--add-dir`, `--agent`, `--process-per-session` (requis pour les serveurs MCP HTTP), `--log-file` et `--disable-telemetry`.

**Cycle de vie protocolaire exact :**
1. **Initialisation & Auth** :
   * Envoyer `initialize` (`protocolVersion: 1`, `clientInfo: {"name": "optigravity", "version": "..."}`). Enregistrer les capacités retournées (notamment `capabilities.session.fork` si présent).
   * Gérer l’erreur `-32000` (non authentifié) en déclenchant `authenticate` (`oauth-personal`) ou en guidant vers `agy acp --login`.
2. **Gestion de sessions** :
   * Utiliser `session/new`, `session/list`, `session/close` et **toujours `session/resume`** (éviter `session/load`, qui rejoue tout l’historique et souffre d’un bug de faux succès sur ID inexistant).
3. **Reconfiguration dynamique** :
   * Utiliser exclusivement **`session/set_config_option`** (ACP v1 stabilisé ; ne jamais appeler les méthodes supprimées `session/set_model` ou `session/set_mode`) pour modifier :
     * `model`
     * `mode` (`default`, `accept-edits`, `auto`, `plan` — **jamais `"yolo"`**, qui fait échouer la session)
     * `thinking_level` / `thought_level` (`Low`, `Medium`, `High`, `Extra`, `Max`)
   * Mettre à jour l’état local depuis la réponse complète des options dépendantes (`config_option_update`).
4. **Exécution, *Steering* et Annulation propre** :
   * Envoyer `session/prompt` (texte, pièces jointes, slash commands v1.5.15+).
   * Un second `session/prompt` concurrent pendant un tour actif permet le **steering** (réorientation en vol).
   * Mapper `Ctrl+C` sur `session/cancel` (libère proprement les outils et permissions pendantes sans tuer le serveur `agy acp`).
5. **Approbations interactives & Subagents** :
   * Implémenter les handlers `session/request_permission` (`allow_once`, `allow_always`, `reject_once`, `reject_always`) et `elicitation/create` (gère aussi les demandes des sous-agents sans blocage de 60 s depuis v1.5.16).
6. **Contournement des 2 lacunes vérifiées d’ACP (`#1045`)** :
   * *Pas de quotas 5h/7j sur le fil ACP* : **interdiction** de poller en boucle `agy -p /usage` en mode OAuth perso ; afficher le dernier snapshot de `quota_snapshots` (et ne rafraîchir que sur action manuelle explicite).
   * *Pas de `num_turns` fiable* (les appels d’outils parallèles d’un même tour sont émis un par un sur le fil) : corréler les événements `usage_update` et `PromptResponse.usage` (`inputTokens`, `outputTokens`, `cachedReadTokens`, `thoughtTokens`, `totalTokens`) avec `transcript_full.jsonl` dans `ogy_core/ingest.py` pour compter les vrais tours logiques.

---

### 2.3 Module 2 — Interface TUI Textual/Rich (`ogy_frontends/acp_tui/app.py`)

Architecture inspirée de `batrachianai/toad`, consommant `ogy_core` directement en mémoire (zéro duplication métier) :

* **Flux principal** : rendu Markdown en streaming (`agent_message_chunk`), blocs de réflexion repliables (`agent_thought_chunk`), cartes d’appels d’outils (`tool_call` / `tool_call_update`) avec diffs inline colorés.
* **Éditeur multiligne** : autocomplétion `@fichier` et commandes `/` (`ogy` + `agy`), raccourcis clavier configurables.
* **Panneau télémétrie live unifié** : affiche en temps réel `thoughtTokens` (inaccessible dans `statusLine` V1), `cachedReadTokens`, taux de cache hit, jauge visuelle de *context rot* (Verte / Ambre / Rouge), coût estimé et budget de la tâche.
* **Barre multi-sessions** : bascule instantanée entre sessions (`session/list`, `session/resume`, `session/close`) et affichage des sous-agents actifs.
* **Modale de permissions** : affichage clair des demandes `session/request_permission` avec rappel des règles *firebreaks* d’`ogy`.

---

### 2.4 Module 3 — Orchestrations dynamiques assistées (Zéro casse de cache)

1. **Sélecteur dynamique Modèle × Effort avec calculateur de *Switch Tax*** :
   * Si `PostToolUse` / `tool_call_update` détecte **2 échecs consécutifs** sur la même commande de test ou ≥ 3 retries en erreur sur un profil `low`/`medium`, la TUI affiche un bandeau d’action rapide (validation en 1 touche).
   * Avant d’appeler `session/set_config_option`, `ogy` calcule et affiche la **Switch Tax** ($H_t \times P_{\text{write}} / 10^6$).
   * **Règle d’or (> 60K tokens)** : si l’historique dépasse 60K tokens, au lieu de changer de modèle dans la session parente (ce qui casserait le cache), `ogy` propose de **déléguer le sous-problème bloquant à un sous-agent ponctuel** sur le modèle fort afin de préserver intact le cache parent.
2. **Rotation transparente de contexte multi-sessions** :
   * Dès que la session atteint la **Zone rouge** (`≥ 100K–120K` tokens, `idle_gap > 300 s`, ou 2 corrections échouées), un bandeau propose en 1 touche (`[R] Rotation propre`) :
     1. Appel à `ogy_write_handoff` pour générer `.ogy/handoff.md` (≤ 2,5K tokens, préservant explicitement les contraintes négatives).
     2. `session/close` sur la session saturée.
     3. `session/new` dans la même fenêtre terminal + injection du handoff au tour 1.

---

### 2.5 Module 4 — Chemin chaud natif `ogy-fast` (Optionnel, si profilage p95 > 25–30 ms)

Si le profilage en production montre que les appels Python stdlib-only de `statusLine` ou `PreToolUse` dépassent 25–30 ms :
* Extraire **uniquement** le sous-module `ogy-hook` (`statusline`, `pre-tool`, `post-tool`, `wrap`) dans un binaire statique compilé en **Rust** (ou **Go**) démarrant en **< 10 ms**, partageant exactement le même schéma SQLite WAL `ogy.db`.
* Option alternative immédiate pour la compression shell : déléguer `ogy wrap` au binaire open-source `rtk-oss` (Apache-2.0) tout en conservant la liste d’exclusion stricte et `ogy_recall`.

---

### 2.6 Jalons & Critères d’acceptation V2 (Semaines 6–10)

* **Livrables :** `ogy tui` (client ACP Textual multi-sessions), panneau télémétrie incluant `thoughtTokens`, sélecteur dynamique assisté avec *Switch Tax*, rotation transparente de session, mode *shadow* / évaluation hors-ligne du bandit Étage 2, et (si besoin) binaire `ogy-fast`.
* **Critères d’acceptation mesurables :**
  * Latence `hook`/`statusLine` p95 **< 10 ms** (si `ogy-fast`) ou **< 25 ms** (Python stdlib).
  * Baisse cumulée **≥ 50 %** du coût par tâche réussie face à un usage `agy` non optimisé en `high` constant, et **≥ 40 %** sur les sessions longues grâce à la rotation transparente.
  * Taux de succès maintenu à **±2 points** du plafond modèle fort.
  * Conformité intégrale ToS Section 6 vérifiée (client fin mono-utilisateur, `clientInfo` explicite, zéro boucle autonome sous `oauth-personal`).

> **Ouverture hors-périmètre V1/V2 (V3 optionnelle — Semaines 11+) :** Si un jour vous passez sur facturation API/Vertex ou disposez d’une machine ≥ 24 Go RAM/VRAM, un module `ogy pipe --backend sdk` (Niveau 5 sur `google-antigravity` Python SDK) pourra se brancher sur le même `ogy_core` pour décharger les handoffs, audits et tris de masse en local à \$0 via **LiteRT Gemma 4 26B** et activer le masquage d’observations en bloc dans `PostToolCallHook`.