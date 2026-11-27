"""Lightweight FR / EN localization for Optigravity V2 TUI (`ogy tui`)."""

from __future__ import annotations

SUPPORTED_LANGS = ("fr", "en")

STRINGS: dict[str, dict[str, str]] = {
    "fr": {
        "connecting": "Connexion à agy acp...",
        "session_none": "Session : aucune",
        "session_bar": "Session {sid} ({count})  ·  Modèle : {model}  ·  Effort : {effort}  ·  Mode : {mode}",
        "apply": "Appliquer",
        "send": "Envoyer",
        "input_placeholder": "Prompt, @fichier, ou /commande (Tab complète · Ctrl+P palette · F1 glossaire)…",
        "section_health": "SANTÉ DU CONTEXTE",
        "section_session": "SESSION",
        "section_quotas": "QUOTAS OAUTH (agy)",
        "section_subagents": "SOUS-AGENTS MCP (API)",
        "metric_api_spend": "API Gemini (USD)",
        "delegate_modal_title": "Déléguer une recherche read-only (> 60K · hors quota OAuth)",
        "delegate_question_label": "Question / objectif pour `ogy_research_repo` :",
        "delegate_scope_label": "Chemins ciblés (optionnel, séparés par des virgules) :",
        "delegate_scope_placeholder": "ex: ogy_core, ogy_subagents",
        "delegate_insert_btn": "Insérer consigne MCP (agy)",
        "delegate_run_btn": "Lancer recherche directe (parallèle)",
        "delegate_running": "Sous-agent `ogy_research_repo` en cours…",
        "delegate_done": "Rapport sous-agent attaché au prompt suivant ({chars} car.).",
        "delegate_inserted": "Consigne de délégation `ogy_research_repo` insérée dans le compositeur.",
        "delegate_failed": "Échec sous-agent read-only : {err}",
        "cmd_delegate": "Ouvrir la modale de délégation read-only (`ogy_research_repo`)",
        "pal_delegate": "Optigravity: Déléguer recherche read-only (/delegate)",
        "pal_delegate_help": "Délègue hors quota OAuth via ogy_research_repo (mode > 60K)",
        "handoff_subagent_used": "Brouillon handoff généré via sous-agent Gemini N1 (`ogy_build_handoff`).",
        "gauge_context": "Contexte",
        "gauge_cache": "Cache prompt",
        "metric_thought": "Raisonnement",
        "metric_io": "Entrée / Sortie",
        "metric_cost": "Coût estimé",
        "metric_idle": "Inactivité",
        "metric_fail": "Échecs outils",
        "quotas_empty": "Aucun quota en cache",
        "glossary_hint": "F1 ou /glossary · Légende",
        "thinking_title": "Raisonnement · {chars} car.",
        "working_thinking": "Réflexion en cours…",
        "working_tool": "Exécution : {name}…",
        "working_responding": "Rédaction de la réponse…",
        "working_sending": "Envoi de la requête…",
        "perm_title": "Autorisation demandée : {tool}",
        "perm_hint": "Conseil Optigravity : privilégiez les lectures bornées et commandes filtrées (ogy wrap).",
        "perm_allow": "Autoriser une fois",
        "perm_reject": "Refuser",
        "glossary_close": "Fermer (Échap)",
        "auth_required": "Authentification requise : {err}",
        "acp_start_failed": "Impossible de démarrer `agy acp` : {err}",
        "prompt_error": "Erreur prompt : {err}",
        "help_header": "Commandes disponibles (Tab pour compléter, Ctrl+P pour palette) :",
        "model_status": "Modèle actuel : {cur} · Disponibles : {avail} (usage : /model <slug>)",
        "model_changed": "Modèle changé → {val}",
        "effort_status": "Effort actuel : {cur} · Disponibles : {avail} (usage : /effort <niveau>)",
        "effort_changed": "Effort changé → {val}",
        "mode_status": "Mode actuel : {cur} · Disponibles : {avail} (usage : /mode <mode>)",
        "mode_changed": "Mode changé → {val}",
        "lang_status": "Langue actuelle : {cur} · Disponibles : fr, en (usage : /lang fr|en)",
        "lang_changed": "Langue changée → {val}",
        "btw_usage": "Usage : /btw <question courte> — pose une question annexe concise sans modifier de fichiers.",
        "handoff_written": "Handoff écrit : {path} ({lines} lignes)",
        "sessions_list": "Sessions récentes : {ids}",
        "sessions_none": "(aucune)",
        "resume_usage": "Usage : /resume <session_id> (tapez /sessions pour voir les IDs)",
        "session_resumed": "Session reprise : {sid}",
        "cmd_unknown": "Commande inconnue : {cmd} (tapez /help ou Ctrl+P)",
        "turn_cancelled": "Tour annulé (`session/cancel`). `agy acp` reste actif.",
        "session_started": "Nouvelle session démarrée : {sid}",
        "session_rotated": "Rotation effectuée → {sid} (handoff : {path})",
        "auto_routed": "Routage auto ({category}) → {model} / {effort} (0 Switch Tax)",
        "upgrade_applied": "Escalade appliquée → {model} / {effort} (Switch Tax ~${tax:.4f})",
        "delegate_hint": (
            "Délégation recommandée ({model}/{effort}) pour préserver le cache parent "
            "(évite Switch Tax ~${tax:.4f}). Ouvrez /new ou demandez à l'agent de déléguer à un sous-agent."
        ),
        # Palette titles
        "pal_glossary": "Optigravity: Glossaire & Légende (/glossary)",
        "pal_glossary_help": "Explique zones, 3p, cache hit, Switch Tax, raccourcis",
        "pal_rotate": "Optigravity: Rotation de session (/rotate)",
        "pal_rotate_help": "Écrit .ogy/handoff.md et ouvre une session fraîche",
        "pal_handoff": "Optigravity: Écrire le handoff (/handoff)",
        "pal_handoff_help": "Sauvegarde .ogy/handoff.md sans fermer la session",
        "pal_new": "Optigravity: Nouvelle session (/new)",
        "pal_new_help": "Démarre une nouvelle session ACP vide",
        "pal_sessions": "Optigravity: Lister les sessions (/sessions)",
        "pal_sessions_help": "Affiche les sessions ACP récentes",
        "pal_cancel": "Optigravity: Annuler le tour en cours (Ctrl+C)",
        "pal_cancel_help": "Envoie session/cancel sans tuer agy acp",
        "pal_telemetry": "Optigravity: Afficher / Masquer télémétrie (Ctrl+B)",
        "pal_telemetry_help": "Bascule la barre latérale droite",
        "pal_help": "Optigravity: Aide des commandes (/help)",
        "pal_help_help": "Liste toutes les commandes slash disponibles",
        "pal_proposal": "Optigravity: Appliquer proposition — {title}",
        "pal_model": "Modèle → {val}",
        "pal_model_help": "Basculer le modèle de la session sur {val}",
        "pal_effort": "Effort → {val}",
        "pal_effort_help": "Régler l'effort de raisonnement sur {val}",
        "pal_mode": "Mode → {val}",
        "pal_mode_help": "Changer le mode d'exécution ACP sur {val}",
        "pal_lang": "Langue / Language → {label}",
        "pal_lang_help": "Passer l'interface en {label}",
        # Slash command descriptions
        "cmd_model": "Changer le modèle actif (ou lister sans argument)",
        "cmd_effort": "Régler l'effort de raisonnement (low..max)",
        "cmd_mode": "Changer le mode d'exécution (default, accept-edits, auto, plan)",
        "cmd_btw": "Poser une question annexe concise sans modifier de fichiers",
        "cmd_lang": "Changer la langue de l'interface (fr | en)",
        "cmd_rotate": "Rotation propre : handoff + nouvelle session fraîche",
        "cmd_handoff": "Écrire .ogy/handoff.md sans fermer la session",
        "cmd_new": "Ouvrir une nouvelle session ACP",
        "cmd_sessions": "Lister les sessions ACP récentes",
        "cmd_resume": "Reprendre une session ACP existante",
        "cmd_clear": "Alias de /rotate (rotation propre)",
        "cmd_glossary": "Afficher la légende et le vocabulaire Optigravity",
        "cmd_help": "Afficher la liste des commandes slash",
        # Phase 2 & V3.6 additions
        "subagents_empty": "Aucun sous-agent actif",
        "new_messages_pill": "↓ Nouveaux messages · End",
        "copied_notice": "Copié dans le presse-papiers ({chars} car.)",
        "copied_code_notice": "Bloc de code copié ({chars} car.)",
        "copy_empty": "Aucun message ou bloc de code à copier dans cette session.",
        "copy_btn": "Copier",
        "copy_code_btn": "Copier code",
        "verbosity_notice": "Verbosité outils : {mode}",
        "pasted_chip": "[Pasted #{idx} · {lines} lignes]",
        "image_chip": "[Image #{idx}]",
        "reverse_search_placeholder": "Recherche historique (Ctrl+R) · Entrée valide · Échap annule…",
        "transcript_search_placeholder": "Rechercher dans la conversation (Ctrl+F) · Entrée suivant · Échap fermer…",
        "transcript_search_none": "0 résultat",
        "transcript_search_count": "{cur}/{total}",
        "file_tree_hint": "Tab : vue arborescente · Entrée : insérer @chemin",
        "file_flat_hint": "Tab : liste fuzzy · Entrée : insérer @chemin",
        "perm_shortcuts": "[Y/1] Autoriser  [A/2] Toujours  [N/Échap] Refuser  [X] Jamais  [F] Plein écran",
        "perm_risk_high": "RISQUE ÉLEVÉ",
        "perm_risk_med": "MODIFICATION",
        "perm_risk_low": "LECTURE",
        "elicit_form_title": "Saisie demandée par l'agent",
        "elicit_url_title": "Authentification / URL externe demandée",
        "elicit_url_host": "Domaine cible : {host}",
        "elicit_url_consent": "Ouvrez uniquement si vous faites confiance à ce domaine. Aucun secret n'est saisi dans le TUI.",
        "elicit_url_open": "Ouvrir dans le navigateur",
        "elicit_url_copy": "Copier l'URL",
        "elicit_accept": "Valider (Ctrl+Entrée)",
        "elicit_decline": "Refuser",
        "elicit_cancel": "Annuler (Échap)",
        "sessions_modal_title": "Sessions ACP (Ctrl+S · /resume)",
        "sessions_search_placeholder": "Filtrer par ID, titre ou modèle…",
        "handoff_modal_title": "Revue du handoff avant rotation (/rotate)",
        "handoff_tokens": "Estimation : ~{tokens} tokens · Ctrl+Entrée pour valider la rotation",
        "handoff_confirm": "Lancer la rotation (Ctrl+Entrée)",
        "v1_switch_notice": "Commande V1 prête : `ogy run --model {model}` (copiée si presse-papiers disponible)",
        "activity_reads": "{n} lecture(s)",
        "activity_searches": "{n} recherche(s)",
        "activity_fetches": "{n} requête(s)",
        "thought_drawer": "Réflexion · {elapsed:.1f}s · {chars} car.",
        "diff_fullscreen": "Plein écran [F]",
        # V3.6 Welcome, Themes, Queueing, Export & Status
        "welcome_title": "Optigravity {version} · Compagnon ACP haute efficience",
        "welcome_meta": "Espace : {workspace}{branch}  ·  Profil : {profile}  ·  Sous-agents : {subagents}",
        "welcome_tips": "Entrée envoyer  ·  Shift+Entrée saut de ligne  ·  @fichier réf.  ·  Ctrl+F chercher  ·  Ctrl+P palette  ·  F1 glossaire",
        "theme_status": "Thème actuel : {cur} · Disponibles : dark, light, high-contrast (usage : /theme <nom>)",
        "theme_changed": "Thème changé → {val}",
        "queue_status": "⏳ {n} en attente : « {preview} »",
        "queue_clear_btn": "Vider",
        "queue_added": "Prompt mis en file d'attente (#{n}) — envoi automatique à la fin du tour.",
        "queue_cleared": "File d'attente vidée ({n} prompt(s)).",
        "export_saved": "Conversation exportée : {path}",
        "export_empty": "Aucun message à exporter dans cette session.",
        "export_failed": "Échec de l'export : {err}",
        "status_summary": "Session {sid} · Profil: {profile} · Modèle: {model} ({effort}/{mode}) · Contexte: {ctx} ({pct}%) · Cache: {cache} · Coût: ${cost:.4f} · API: ${api:.4f} ({runs} runs)",
        "cmd_theme": "Changer le thème visuel (dark | light | high-contrast)",
        "cmd_copy": "Copier la dernière réponse ou le dernier bloc de code (/copy [code])",
        "cmd_export": "Exporter la session active en Markdown (/export [chemin])",
        "cmd_status": "Afficher le résumé télémétrie de la session active",
        "cmd_find": "Rechercher dans la conversation active (Ctrl+F)",
        "cmd_clear_queue": "Vider les prompts en file d'attente",
        "pal_theme": "Thème → {val}",
        "pal_theme_help": "Basculer le thème visuel sur {val}",
        "pal_copy_last": "Optigravity: Copier dernière réponse (/copy)",
        "pal_copy_last_help": "Copie la dernière réponse assistant dans le presse-papiers",
        "pal_copy_code": "Optigravity: Copier dernier bloc de code (/copy code)",
        "pal_copy_code_help": "Copie le dernier bloc ```...``` sans les balises Markdown",
        "pal_export": "Optigravity: Exporter la session en Markdown (/export)",
        "pal_export_help": "Sauvegarde le transcript actif dans .ogy/exports/",
        "pal_find": "Optigravity: Rechercher dans le transcript (Ctrl+F)",
        "pal_find_help": "Ouvre la barre de recherche intra-conversation",
        "pal_status": "Optigravity: Résumé de session (/status)",
        "pal_status_help": "Affiche la télémétrie et le ROI de la session active",
    },
    "en": {
        "connecting": "Connecting to agy acp...",
        "session_none": "Session: none",
        "session_bar": "Session {sid} ({count})  ·  Model: {model}  ·  Effort: {effort}  ·  Mode: {mode}",
        "apply": "Apply",
        "send": "Send",
        "input_placeholder": "Prompt, @file, or /command (Tab completes · Ctrl+P palette · F1 glossary)…",
        "section_health": "CONTEXT HEALTH",
        "section_session": "SESSION",
        "section_quotas": "OAUTH QUOTAS (agy)",
        "section_subagents": "MCP SUBAGENTS (API)",
        "metric_api_spend": "Gemini API (USD)",
        "delegate_modal_title": "Delegate read-only research (> 60K · off OAuth quota)",
        "delegate_question_label": "Question / goal for `ogy_research_repo`:",
        "delegate_scope_label": "Scope paths (optional, comma-separated):",
        "delegate_scope_placeholder": "e.g. ogy_core, ogy_subagents",
        "delegate_insert_btn": "Insert MCP instruction (agy)",
        "delegate_run_btn": "Run research directly (parallel)",
        "delegate_running": "Running `ogy_research_repo` subagent…",
        "delegate_done": "Subagent sourced report attached to next prompt ({chars} chars).",
        "delegate_inserted": "Delegation instruction for `ogy_research_repo` inserted into composer.",
        "delegate_failed": "Read-only subagent failed: {err}",
        "cmd_delegate": "Open read-only delegation modal (`ogy_research_repo`)",
        "pal_delegate": "Optigravity: Delegate read-only research (/delegate)",
        "pal_delegate_help": "Delegate off OAuth quota via ogy_research_repo (> 60K mode)",
        "handoff_subagent_used": "Handoff draft generated via Gemini N1 subagent (`ogy_build_handoff`).",
        "gauge_context": "Context",
        "gauge_cache": "Prompt Cache",
        "metric_thought": "Reasoning",
        "metric_io": "Input / Output",
        "metric_cost": "Est. Cost",
        "metric_idle": "Idle Gap",
        "metric_fail": "Tool Failures",
        "quotas_empty": "No cached quotas yet",
        "glossary_hint": "F1 or /glossary · Legend",
        "thinking_title": "Reasoning · {chars} chars",
        "working_thinking": "Thinking…",
        "working_tool": "Running: {name}…",
        "working_responding": "Writing response…",
        "working_sending": "Sending prompt…",
        "perm_title": "Permission requested: {tool}",
        "perm_hint": "Optigravity tip: prefer bounded reads and wrapped shell commands (ogy wrap).",
        "perm_allow": "Allow once",
        "perm_reject": "Reject",
        "glossary_close": "Close (Esc)",
        "auth_required": "Authentication required: {err}",
        "acp_start_failed": "Failed to start `agy acp`: {err}",
        "prompt_error": "Prompt error: {err}",
        "help_header": "Available commands (Tab to complete, Ctrl+P for palette):",
        "model_status": "Current model: {cur} · Available: {avail} (usage: /model <slug>)",
        "model_changed": "Model switched → {val}",
        "effort_status": "Current effort: {cur} · Available: {avail} (usage: /effort <level>)",
        "effort_changed": "Effort switched → {val}",
        "mode_status": "Current mode: {cur} · Available: {avail} (usage: /mode <mode>)",
        "mode_changed": "Mode switched → {val}",
        "lang_status": "Current language: {cur} · Available: fr, en (usage: /lang fr|en)",
        "lang_changed": "Language switched → {val}",
        "btw_usage": "Usage: /btw <short question> — asks a concise side question without modifying files.",
        "handoff_written": "Handoff written: {path} ({lines} lines)",
        "sessions_list": "Recent sessions: {ids}",
        "sessions_none": "(none)",
        "resume_usage": "Usage: /resume <session_id> (run /sessions to list IDs)",
        "session_resumed": "Resumed session: {sid}",
        "cmd_unknown": "Unknown command: {cmd} (type /help or press Ctrl+P)",
        "turn_cancelled": "Turn cancelled (`session/cancel`). `agy acp` remains active.",
        "session_started": "Started new session: {sid}",
        "session_rotated": "Rotated to {sid} (handoff: {path})",
        "auto_routed": "Auto-routed ({category}) → {model} / {effort} (0 Switch Tax)",
        "upgrade_applied": "Upgraded → {model} / {effort} (Switch Tax ~${tax:.4f})",
        "delegate_hint": (
            "Delegation recommended ({model}/{effort}) to preserve parent cache "
            "(avoids Switch Tax ~${tax:.4f}). Open /new or ask the agent to delegate to a subagent."
        ),
        # Palette titles
        "pal_glossary": "Optigravity: Glossary & Legend (/glossary)",
        "pal_glossary_help": "Explains zones, 3p, cache hit, Switch Tax, shortcuts",
        "pal_rotate": "Optigravity: Rotate Session (/rotate)",
        "pal_rotate_help": "Writes .ogy/handoff.md and opens a fresh session",
        "pal_handoff": "Optigravity: Write Handoff (/handoff)",
        "pal_handoff_help": "Saves .ogy/handoff.md without closing the session",
        "pal_new": "Optigravity: New Session (/new)",
        "pal_new_help": "Starts a fresh ACP session",
        "pal_sessions": "Optigravity: List Sessions (/sessions)",
        "pal_sessions_help": "Shows recent ACP sessions",
        "pal_cancel": "Optigravity: Cancel Turn (Ctrl+C)",
        "pal_cancel_help": "Sends session/cancel without killing agy acp",
        "pal_telemetry": "Optigravity: Toggle Telemetry (Ctrl+B)",
        "pal_telemetry_help": "Shows or hides the right telemetry sidebar",
        "pal_help": "Optigravity: Command Help (/help)",
        "pal_help_help": "Lists all available slash commands",
        "pal_proposal": "Optigravity: Apply Proposal — {title}",
        "pal_model": "Model → {val}",
        "pal_model_help": "Switch session model to {val}",
        "pal_effort": "Effort → {val}",
        "pal_effort_help": "Set reasoning effort to {val}",
        "pal_mode": "Mode → {val}",
        "pal_mode_help": "Switch ACP execution mode to {val}",
        "pal_lang": "Language / Langue → {label}",
        "pal_lang_help": "Switch UI language to {label}",
        # Slash command descriptions
        "cmd_model": "Switch active model (or list available without argument)",
        "cmd_effort": "Set reasoning effort (low..max)",
        "cmd_mode": "Switch execution mode (default, accept-edits, auto, plan)",
        "cmd_btw": "Ask a concise side question without modifying files",
        "cmd_lang": "Switch UI language (fr | en)",
        "cmd_rotate": "Clean rotation: handoff + fresh session",
        "cmd_handoff": "Write .ogy/handoff.md without closing session",
        "cmd_new": "Open a new ACP session",
        "cmd_sessions": "List recent ACP sessions",
        "cmd_resume": "Resume an existing ACP session",
        "cmd_clear": "Alias for /rotate (clean rotation)",
        "cmd_glossary": "Open the Optigravity metrics & vocabulary glossary",
        "cmd_help": "Show slash commands help",
        # Phase 2 & V3.6 additions
        "subagents_empty": "No active subagents",
        "new_messages_pill": "↓ New messages · End",
        "copied_notice": "Copied to clipboard ({chars} chars)",
        "copied_code_notice": "Code block copied ({chars} chars)",
        "copy_empty": "No assistant message or code block to copy in this session.",
        "copy_btn": "Copy",
        "copy_code_btn": "Copy code",
        "verbosity_notice": "Tool verbosity: {mode}",
        "pasted_chip": "[Pasted #{idx} · {lines} lines]",
        "image_chip": "[Image #{idx}]",
        "reverse_search_placeholder": "Reverse history search (Ctrl+R) · Enter accepts · Esc cancels…",
        "transcript_search_placeholder": "Search conversation (Ctrl+F) · Enter next · Esc close…",
        "transcript_search_none": "0 matches",
        "transcript_search_count": "{cur}/{total}",
        "file_tree_hint": "Tab: tree view · Enter: insert @path",
        "file_flat_hint": "Tab: fuzzy list · Enter: insert @path",
        "perm_shortcuts": "[Y/1] Allow  [A/2] Always  [N/Esc] Reject  [X] Reject always  [F] Fullscreen",
        "perm_risk_high": "HIGH RISK",
        "perm_risk_med": "EDIT",
        "perm_risk_low": "READ",
        "elicit_form_title": "Agent input requested",
        "elicit_url_title": "External URL / auth requested",
        "elicit_url_host": "Target domain: {host}",
        "elicit_url_consent": "Open only if you trust this domain. Secrets are never collected in a TUI form.",
        "elicit_url_open": "Open in browser",
        "elicit_url_copy": "Copy URL",
        "elicit_accept": "Submit (Ctrl+Enter)",
        "elicit_decline": "Decline",
        "elicit_cancel": "Cancel (Esc)",
        "sessions_modal_title": "ACP Sessions (Ctrl+S · /resume)",
        "sessions_search_placeholder": "Filter by ID, title, or model…",
        "handoff_modal_title": "Review handoff before rotation (/rotate)",
        "handoff_tokens": "Estimated: ~{tokens} tokens · Ctrl+Enter to confirm rotation",
        "handoff_confirm": "Confirm rotation (Ctrl+Enter)",
        "v1_switch_notice": "V1 command ready: `ogy run --model {model}` (copied if clipboard available)",
        "activity_reads": "{n} read(s)",
        "activity_searches": "{n} search(es)",
        "activity_fetches": "{n} fetch(es)",
        "thought_drawer": "Reasoning · {elapsed:.1f}s · {chars} chars",
        "diff_fullscreen": "Fullscreen [F]",
        # V3.6 Welcome, Themes, Queueing, Export & Status
        "welcome_title": "Optigravity {version} · High-efficiency ACP Companion",
        "welcome_meta": "Workspace: {workspace}{branch}  ·  Profile: {profile}  ·  Subagents: {subagents}",
        "welcome_tips": "Enter send  ·  Shift+Enter newline  ·  @file ref  ·  Ctrl+F search  ·  Ctrl+P palette  ·  F1 glossary",
        "theme_status": "Current theme: {cur} · Available: dark, light, high-contrast (usage: /theme <name>)",
        "theme_changed": "Theme switched → {val}",
        "queue_status": "⏳ {n} queued: \"{preview}\"",
        "queue_clear_btn": "Clear",
        "queue_added": "Prompt queued (#{n}) — will dispatch automatically when the active turn finishes.",
        "queue_cleared": "Prompt queue cleared ({n} prompt(s)).",
        "export_saved": "Conversation exported: {path}",
        "export_empty": "No messages to export in this session.",
        "export_failed": "Export failed: {err}",
        "status_summary": "Session {sid} · Profile: {profile} · Model: {model} ({effort}/{mode}) · Context: {ctx} ({pct}%) · Cache: {cache} · Cost: ${cost:.4f} · API: ${api:.4f} ({runs} runs)",
        "cmd_theme": "Switch visual theme (dark | light | high-contrast)",
        "cmd_copy": "Copy last assistant response or last code block (/copy [code])",
        "cmd_export": "Export active session to Markdown (/export [path])",
        "cmd_status": "Show inline telemetry summary for the active session",
        "cmd_find": "Search inside the active conversation (Ctrl+F)",
        "cmd_clear_queue": "Clear queued prompts for this session",
        "pal_theme": "Theme → {val}",
        "pal_theme_help": "Switch UI theme to {val}",
        "pal_copy_last": "Optigravity: Copy Last Response (/copy)",
        "pal_copy_last_help": "Copy the latest assistant message to the clipboard",
        "pal_copy_code": "Optigravity: Copy Last Code Block (/copy code)",
        "pal_copy_code_help": "Copy the latest ```...``` code block without Markdown fences",
        "pal_export": "Optigravity: Export Session to Markdown (/export)",
        "pal_export_help": "Save the active transcript to .ogy/exports/",
        "pal_find": "Optigravity: Search Transcript (Ctrl+F)",
        "pal_find_help": "Open the in-conversation search bar",
        "pal_status": "Optigravity: Session Status (/status)",
        "pal_status_help": "Display live telemetry and ROI for the active session",
    },
}

ZONE_LABELS: dict[str, dict[tuple[str, str], tuple[str, str]]] = {
    "fr": {
        ("green", "keep_warm"): ("SAIN · Cache chaud", "Contexte optimal, continuez normalement"),
        ("amber", "frugal_btw"): ("ATTENTION · Rester frugal", "60K–100K tok ou cache <60% · utilisez /btw"),
        ("red", "clear_handoff"): ("SATURÉ · Rotation conseillée", "Contexte plein ou cache froid · /rotate"),
        ("red", "compact"): ("SATURÉ · Compaction", "Compaction rentable si plusieurs tours restent"),
        ("red", "rewind"): ("ÉCHECS · Revenir en arrière", "Échecs répétés · ne pas compacter les erreurs"),
    },
    "en": {
        ("green", "keep_warm"): ("HEALTHY · Warm cache", "Optimal context, continue normally"),
        ("amber", "frugal_btw"): ("CAUTION · Stay frugal", "60K–100K tok or cache <60% · use /btw"),
        ("red", "clear_handoff"): ("SATURATED · Rotate advised", "Context full or cold cache · /rotate"),
        ("red", "compact"): ("SATURATED · Compact", "Compaction breaks even if enough turns remain"),
        ("red", "rewind"): ("FAILURES · Rewind", "Repeated errors · never compact failures"),
    },
}

QUOTA_LABELS: dict[str, dict[str, str]] = {
    "fr": {
        "gemini-5h": "Gemini (5h)",
        "gemini-weekly": "Gemini (7j)",
        "3p-5h": "Tiers / Claude (5h)",
        "3p-weekly": "Tiers / Claude (7j)",
        "claude-5h": "Claude (5h)",
        "claude-weekly": "Claude (7j)",
    },
    "en": {
        "gemini-5h": "Gemini (5h)",
        "gemini-weekly": "Gemini (7d)",
        "3p-5h": "3rd-Party / Claude (5h)",
        "3p-weekly": "3rd-Party / Claude (7d)",
        "claude-5h": "Claude (5h)",
        "claude-weekly": "Claude (7d)",
    },
}

GLOSSARY_MARKDOWN: dict[str, str] = {
    "fr": """\
# Vocabulaire & Indicateurs Optigravity

## Santé du contexte (Zones)
- **SAIN (`keep_warm`)** : `< 60K` tokens et cache actif. Gardez la session chaude pour profiter du tarif cache réduit.
- **ATTENTION (`frugal_btw`)** : `60K–100K` tokens, `15–25` tours, ou cache `< 60%`. Évitez les lectures inutiles et utilisez **`/btw <question>`** pour les questions annexes courtes.
- **SATURÉ (`clear_handoff`)** : `>= 100K` tokens ou cache expiré (`> 180s` d'inactivité au-delà de 60K). Utilisez **`/rotate`** (ou `Alt+R`) pour repartir sur une session neuve via `.ogy/handoff.md`.
- **SATURÉ (`compact`)** : compaction rentable uniquement s'il reste suffisamment de tours sans erreurs.
- **ÉCHECS (`rewind`)** : `>= 2` échecs d'outils consécutifs. Ne compactez jamais des erreurs ; changez d'approche ou faites une rotation.

## Métriques de la barre latérale
- **Contexte** : taille actuelle du contexte par rapport à la fenêtre maximale du modèle.
- **Cache prompt** : pourcentage de tokens lus depuis le cache (coût réduit jusqu'à 90%).
- **Raisonnement (`Thought Tokens`)** : tokens de réflexion interne du modèle (facturés au tarif sortie).
- **Inactivité (`Idle Gap`)** : temps écoulé depuis le dernier tour. Au-delà de **180 s**, le cache implicite Gemini risque l'éviction.
- **Switch Tax** : coût d'invalidation du cache lorsqu'on change de modèle/effort en milieu de session chaude. Au-delà de `60K` tokens, déléguer à un sous-agent est moins cher.

## Quotas
- **Gemini (5h / 7j)** : enveloppe de quota des modèles Gemini (`gemini-5h`, `gemini-weekly`).
- **Tiers / Claude (`3p-5h` / `3p-weekly`)** : **`3p` = Third-Party** (modèles tiers comme Claude 5.5 inclus dans votre abonnement Google AI Pro/Ultra, avec quota distinct de Gemini).

## Raccourcis & Commandes utiles
- `/theme dark|light|high-contrast` : changer le thème visuel
- `/lang fr` ou `/lang en` : changer la langue de l'interface
- `/btw <question>` : poser une question annexe concise sans modifier de fichiers
- `/copy [code]` · `/export [chemin]` · `/status` : copier, exporter en Markdown, ou résumer la session
- `Ctrl+P` : Palette de commandes complète (modèles, modes, efforts, thèmes, langue, rotation, glossaire…)
- `Ctrl+F` : Rechercher dans la conversation active
- `Ctrl+R` : Recherche inverse dans l'historique des prompts
- `Ctrl+S` : Sélecteur de sessions fuzzy (`/resume`)
- `Ctrl+O` : Basculer la verbosité des cartes outils
- `Ctrl+B` : Afficher / masquer le panneau télémétrie
- `Ctrl+C` : Annuler le tour en cours sans fermer `agy acp`
- `Alt+R` / `Alt+D` / `Alt+U` : Appliquer une proposition d'orchestration (rotation, délégation, escalade)
- `F1` : Ouvrir / fermer ce glossaire
""",
    "en": """\
# Optigravity Vocabulary & Metrics

## Context Health (Zones)
- **HEALTHY (`keep_warm`)**: `< 60K` tokens with warm cache. Keep the session warm for discounted cached input.
- **CAUTION (`frugal_btw`)**: `60K–100K` tokens, `15–25` turns, or cache `< 60%`. Avoid unnecessary reads and use **`/btw <question>`** for brief side questions.
- **SATURATED (`clear_handoff`)**: `>= 100K` tokens or cold cache (`> 180s` idle above 60K). Run **`/rotate`** (or `Alt+R`) to start fresh via `.ogy/handoff.md`.
- **SATURATED (`compact`)**: compaction breaks even only if enough error-free turns remain.
- **FAILURES (`rewind`)**: `>= 2` consecutive tool errors. Never compact failures; change strategy or rotate.

## Sidebar Metrics
- **Context**: current context size vs the model's maximum context window.
- **Prompt Cache**: percentage of prompt tokens served from cache (up to 90% cheaper).
- **Reasoning (`Thought Tokens`)**: internal thinking tokens (billed at output rate).
- **Idle Gap**: time since the last completed turn. Beyond **180 s**, Gemini's implicit cache risks eviction.
- **Switch Tax**: cache invalidation cost incurred when switching model/effort mid-session. Above `60K` tokens, delegating to a subagent is cheaper.

## Quotas
- **Gemini (5h / 7d)**: Gemini quota buckets (`gemini-5h`, `gemini-weekly`).
- **3rd-Party / Claude (`3p-5h` / `3p-weekly`)**: **`3p` = Third-Party** (non-Gemini models such as Claude 5.5 included in Google AI Pro/Ultra, tracked separately).

## Useful Shortcuts & Commands
- `/theme dark|light|high-contrast`: switch visual theme
- `/lang en` or `/lang fr`: switch UI language
- `/btw <question>`: ask a concise side question without editing files
- `/copy [code]` · `/export [path]` · `/status`: copy, export to Markdown, or inspect session summary
- `Ctrl+P`: Full Command Palette (models, modes, efforts, themes, language, rotation, glossary…)
- `Ctrl+F`: Search inside the active conversation
- `Ctrl+R`: Reverse prompt history search
- `Ctrl+S`: Fuzzy session switcher (`/resume`)
- `Ctrl+O`: Toggle tool card verbosity
- `Ctrl+B`: Toggle telemetry sidebar
- `Ctrl+C`: Cancel the active turn without killing `agy acp`
- `Alt+R` / `Alt+D` / `Alt+U`: Apply an orchestration proposal (rotate, delegate, upgrade)
- `F1`: Open / close this glossary
""",
}


def t(lang: str, key: str, **kwargs: object) -> str:
    """Translate `key` for `lang` ('fr' or 'en') with optional formatting."""
    norm = lang if lang in STRINGS else "fr"
    template = STRINGS[norm].get(key) or STRINGS["fr"].get(key) or key
    return template.format(**kwargs) if kwargs else template


def human_quota_label(bucket_id: str, lang: str = "fr") -> str:
    norm = lang if lang in QUOTA_LABELS else "fr"
    table = QUOTA_LABELS[norm]
    if bucket_id in table:
        return table[bucket_id]
    if norm == "fr":
        cleaned = bucket_id.replace("3p-", "Tiers ").replace("weekly", "7j").replace("-", " ")
    else:
        cleaned = bucket_id.replace("3p-", "3rd-Party ").replace("weekly", "7d").replace("-", " ")
    return cleaned.capitalize()
