# Utiliser l'API Gemini (Free Tier & Crédits Google AI Pro) dans Antigravity

## Contrainte d'architecture fondamentale

Les sous-agents natifs d'Antigravity (définis dans `.agents/agents/*.md` ou `~/.gemini/config/agents/`) **héritent systématiquement de l'authentification de la session principale**. Il n'est pas possible de leur assigner une clé API différente directement dans leur fichier de configuration.

Deux stratégies permettent de contourner cette limite selon ton objectif :
1. **Option A (Session dédiée)** : basculer l'intégralité d'une session Antigravity CLI sur ta clé API Gemini.
2. **Option B (Architecture hybride)** : conserver ton abonnement Google AI Pro (`oauth-personal`) sur l'agent principal et déléguer les tâches légères ou volumineuses à un **serveur MCP local** authentifié via `GEMINI_API_KEY`.

---

## 1. Préparer ses clés API : Free Tier vs Crédits Google AI Pro

Dans **Google AI Studio**, crée **deux projets distincts** afin de séparer strictement tes usages et éviter de consommer tes crédits Cloud par erreur.

### Projet 1 : Free Tier (0 €)
* Crée un projet **sans aucun compte de facturation lié**.
* **Point d'attention :** certains modèles (en période de surcharge ou en version expérimentale) peuvent renvoyer une erreur `429 RESOURCE_EXHAUSTED` (quota à `0`) sur le Free Tier.
* **Vérification :** consulte tes limites réellement actives dans l'onglet **Rate limits** de Google AI Studio.

### Projet 2 : Crédits Google AI Pro (Tier 1)
* **Avantage inclus :** ton abonnement Google AI Pro inclut **10 $/mois de crédits Google Cloud** via le *Google Developer Program*, cumulables sur **12 mois glissants**.
* **Activation :** rends-toi sur `aistudio.google.com/billing` et réclame tes crédits en un clic (ton profil *Google Developer Program* est créé automatiquement si besoin).
* **Prérequis bloquant :** pour débloquer l'utilisation de ces crédits promotionnels sur l'API Gemini, Google impose un **prépaiement / solde minimum de 5 $** sur le compte de facturation lié *(offre réservée à l'abonné principal)*.
* **Ordre de grandeur tarifaire (`gemini-3.5-flash-lite`) :**
  * Entrée : **0,30 $ / 1M tokens**
  * Sortie : **2,50 $ / 1M tokens**
  * Les 10 $/mois couvrent donc un usage très intensif en sous-agent.
* **Vérification :** dans `aistudio.google.com/billing`, le solde de crédits apparaît comme actif et le projet bascule en **Tier 1** dans la page *Projects* d'AI Studio.

---

## 2. Option A : Basculer toute la session Antigravity sur une clé API

Cette approche permet d'exécuter une session complète sans consommer ton quota Google AI Pro.

### Configuration
1. Modifie `~/.gemini/antigravity-cli/settings.json` pour forcer le fournisseur `gemini` :
   ```json
   {
     "modelProvider": "gemini"
   }
   ```
   > **Note (Extensions IDE / ACP) :** le champ équivalent pour les extensions IDE ou ACP est :
   > `"auth": { "type": "gemini-api-key" }`

2. Exporte ta clé API dans l'environnement du shell (`~/.bashrc` ou `~/.zshrc`) :
   ```bash
   export GEMINI_API_KEY="ta_cle_ai_studio"
   ```
   > **Attention :** Antigravity ignore la variable `GOOGLE_API_KEY` ainsi que les fichiers `.env` locaux. La variable `GEMINI_API_KEY` doit impérativement être exportée dans le shell.

3. Lance le CLI avec `agy`.
   * **Vérification :** l'écran de connexion OAuth est sauté et l'en-tête du CLI affiche **`Gemini API key`** au lieu de ton adresse e-mail.

### Déclarer un sous-agent natif dédié
Crée un fichier dans `.agents/agents/fast-worker.md` (projet) ou `~/.gemini/config/agents/` (global) :

```markdown
---
model: gemini-3.5-flash-lite
subagent: true
mainAgent: false
tools:
  - view_file
  - run_command
---
# Core Instructions
Tu es un sous-agent rapide chargé d'analyser les logs, résumer des fichiers ou exécuter des vérifications simples.
```

### Revenir à l'abonnement Google AI Pro
Supprime simplement la ligne `"modelProvider"` dans `~/.gemini/antigravity-cli/settings.json` et relance `agy`.

---

## 3. Option B : Setup hybride (Google AI Pro + Sous-agent MCP)

Un serveur MCP local agit comme une boîte noire pour Antigravity : il dispose de son propre environnement et de sa propre clé API, totalement isolés de la session OAuth principale.

| Composant | Authentification | Rôle recommandé |
| :--- | :--- | :--- |
| **Agent principal Antigravity** | `oauth-personal` (Google AI Pro) | Architecture, planification, édition complexe, orchestration |
| **Sous-agent via serveur MCP** | `GEMINI_API_KEY` (Free Tier ou Crédits) | Extraction, tri de fichiers, analyse de logs, génération/exécution de tests, tâches répétitives |

### Déclaration du serveur MCP
Ajoute le serveur dans `~/.gemini/config/mcp_config.json` (global, recommandé) ou `.agents/mcp_config.json` (projet) :

```json
{
  "mcpServers": {
    "flash-lite-worker": {
      "command": "python3",
      "args": ["/chemin/vers/ton_serveur_mcp.py"],
      "env": {
        "GEMINI_API_KEY": "ta_cle_ai_studio_gratuite_ou_credits"
      }
    }
  }
}
```

> **Sécurité Git :** ne commite jamais `.agents/mcp_config.json` s'il contient la clé en clair. Privilégie le fichier global `~/.gemini/config/mcp_config.json` ou l'héritage depuis les variables d'environnement du shell.

### Vérification et orchestration
1. Redémarre Antigravity CLI ou l'extension IDE.
2. Tape `/mcp` dans le CLI (ou ouvre **MCP Servers** via le menu `…` du panneau agent dans l'IDE) : `flash-lite-worker` doit apparaître comme connecté avec ses outils listés.
3. Ajoute une **Rule** ou un **Skill** dans Antigravity pour indiquer à l'agent principal quand déléguer les tâches de volume à cet outil MCP.

### Paramétrage spécifique à `gemini-3.5-flash-lite`
* **`thinking_level` :**
  * `"minimal"` pour de la simple extraction ou du résumé.
  * `"medium"` ou `"high"` si le sous-agent enchaîne des appels d'outils ou de l'exécution de code.
* **Paramètres dépréciés :** ne pas envoyer `temperature`, `top_p` ou `top_k` (dépréciés sur cette génération de modèles).

---

## 4. Les 3 niveaux d'autonomie d'un sous-agent MCP

Un serveur MCP n'est pas limité à un simple appel *prompt → texte* : il peut héberger une véritable boucle agentique autonome.

| Architecture | Fonctionnement interne | Effort de code | Contrôle & Sécurité |
| :--- | :--- | :--- | :--- |
| **1. Stateless** | 1 appel API `generate_content` (texte entrant → texte sortant). | Minimal (~20 lignes) | Total (aucun accès disque ni shell) |
| **2. Boucle agentique Python** | Le MCP utilise `google-genai` (ou `google-antigravity`) avec **Function Calling** sur tes propres outils locaux (`read_file`, `write_file`, `run_cmd`). | Moyen (~80 lignes) | Fin (tu maîtrises exactement les outils exposés) |
| **3. Wrapper CLI Headless** | Le MCP lance un sous-processus agentique complet (`agy -p` ou `gemini` CLI) avec `GEMINI_API_KEY`. | Faible (~40 lignes) | Dépend des règles de permissions du CLI invoqué |

---

### Niveau 1 : Stateless
Idéal pour résumer des logs, extraire du JSON structuré ou classer du texte sans donner d'accès au système de fichiers. Une simple fonction `@mcp.tool()` appelle `client.models.generate_content(...)` et renvoie `response.text`.

### Niveau 2 : Boucle agentique Python (`FastMCP` + `google-genai`)
Le SDK officiel `google-genai` gère nativement l'**Automatic Function Calling** : il exécute lui-même la boucle *(appel modèle → exécution de la fonction Python locale → renvoi du résultat au modèle → nouvel appel)* jusqu'à résolution complète de la tâche.

```python
import subprocess
from pathlib import Path
from mcp.server.fastmcp import FastMCP
from google import genai
from google.genai import types

mcp = FastMCP("flash-lite-agent")
client = genai.Client()  # Lit GEMINI_API_KEY injectée par mcp_config.json

# Outils locaux exposés uniquement au sous-agent
def read_file(path: str) -> str:
    """Lit un fichier texte dans le workspace."""
    return Path(path).read_text(encoding="utf-8")

def write_file(path: str, content: str) -> str:
    """Écrit ou remplace un fichier dans le workspace."""
    Path(path).write_text(content, encoding="utf-8")
    return f"OK: {path} écrit."

def run_pytest(target: str = ".") -> str:
    """Lance pytest et retourne stdout/stderr."""
    res = subprocess.run(["pytest", target], capture_output=True, text=True, timeout=30)
    return f"Exit {res.returncode}\n{res.stdout}\n{res.stderr}"

@mcp.tool()
def delegate_subagent(task: str) -> str:
    """Délègue une tâche autonome (lecture, édition, tests) à Gemini 3.5 Flash-Lite."""
    response = client.models.generate_content(
        model="gemini-3.5-flash-lite",
        contents=task,
        config=types.GenerateContentConfig(
            system_instruction=(
                "Tu es un sous-agent autonome. Utilise tes outils pour accomplir la tâche "
                "et renvoie un rapport concis listant exactement tous les fichiers créés ou modifiés."
            ),
            tools=[read_file, write_file, run_pytest],
            thinking_config=types.ThinkingConfig(thinking_level="medium"),
            automatic_function_calling=types.AutomaticFunctionCallingConfig(
                maximum_remote_calls=10
            ),
        ),
    )
    return response.text
```

> **Aller plus loin :** pour des workflows plus avancés (mémoire de session persistante, plusieurs sous-agents chaînés), le framework **Google ADK** s'intègre directement à l'intérieur d'une fonction `@mcp.tool()`.

### Niveau 3 : Wrapper CLI Headless
Permet d'éviter de recoder les outils classiques (`grep`, édition par diff, exploration de codebase) en encapsulant un CLI agentique existant dans un sous-processus.

* **Avec `agy` en mode headless :**
  ```bash
  agy -p "Ta tâche" --model gemini-3.5-flash-lite --output-format stream-json
  ```
  * **Point de vigilance :** `agy` lit `modelProvider` dans `~/.gemini/antigravity-cli/settings.json`. Lancer `agy` en sous-processus avec une clé API alors que ta session principale tourne en OAuth peut créer un conflit de configuration.
  * **Alternative propre :** encapsuler un CLI distinct comme **Gemini CLI**, qui accepte directement `GEMINI_API_KEY` en variable d'environnement sans toucher à la config d'Antigravity.
* **Gestion des permissions en headless :** aucune interaction manuelle ("Accepter") n'est possible dans le terminal.
  * Utiliser un mode d'approbation explicite comme `--mode=auto` combiné au sandbox terminal, ou des règles `permissions.allow` ciblées.
  * **Éviter** `--dangerously-skip-permissions` en dehors d'un environnement strictement isolé (conteneur/VM).

---

## 5. Pièges en production et bonnes pratiques

* **Synchronisation et conflits de fichiers :** l'agent principal Antigravity ne voit pas les modifications disque en temps réel pendant l'exécution d'un appel MCP. Impose dans le `system_instruction` du sous-agent MCP qu'il retourne **systématiquement la liste exacte des fichiers créés ou modifiés**, afin que l'agent principal sache quoi relire avant d'éditer.
* **Boucles infinies :** un modèle léger peut boucler sur une erreur de test qu'il n'arrive pas à corriger. Plafonne toujours le nombre d'appels d'outils avec `AutomaticFunctionCallingConfig(maximum_remote_calls=10)` dans `google-genai`.
* **Blocage de session (Timeouts) :** ajoute systématiquement un `timeout` sur chaque commande shell exécutée par le sous-agent (ex. `subprocess.run(..., timeout=30)`) pour éviter de geler la session Antigravity principale.