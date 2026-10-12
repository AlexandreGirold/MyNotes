# Architecture technique de MyNotes

Source du schéma : [`architecture.mmd`](architecture.mmd).

Version rendue et syntaxe validée : [`architecture.svg`](architecture.svg).
Image : [`architecture.png`](architecture.png). Utiliser le SVG pour zoomer sur les détails.

Dans [Mermaid.ai](https://mermaid.ai/), ouvrir l'éditeur Mermaid et coller le contenu du fichier `.mmd` pour afficher et modifier le schéma. Ce fichier est importable ; il n'a pas été publié sur un compte externe.

## Lecture

- **Cyan** : application Electron, interface React et capture microphone.
- **Vert** : backend Python, traitements, outils et assistant OpenCode.
- **Violet** : stockage local, dont une seule base SQLite avec recherche FTS5.
- **Rose** : secrets et contrôle de l'accès aux dossiers.
- **Orange** : services externes et téléchargement initial des modèles.
- **Gris pointillé** : diarisation locale optionnelle ; fonctionnement validé avec les modèles réels, précision sur longues discussions non mesurée.

Les modules d'import, d'export, de notes et de sauvegarde sont des composants du même backend, pas des serveurs séparés. Le bridge MCP et les processus OpenCode/diarisation partagent le stockage de l'application ; ils n'ajoutent aucune base, aucun broker et aucun serveur de recherche.

Les cours et présentations sont inclus car leur interface et leur plomberie existent. Leur réparation fonctionnelle reste hors du correctif audio courant. La diarisation nécessite de conserver temporairement l'audio, puis de relier les tours de parole au texte Voxtral ; elle ne remplace pas Voxtral. Les embeddings de voix restent internes à ce traitement et ne servent jamais à la recherche documentaire.

## Sources vérifiées

- `src/ui/App.tsx`, `src/ui/components/LiveNotes.tsx`, `src/ui/pcm-worklet.js`.
- `electron/main.cjs`, `electron/preload.cjs`.
- `src/mynotes/api.py`, `src/mynotes/__main__.py`, `src/mynotes/db.py`, `src/mynotes/worker.py`.
- `src/mynotes/importers.py`, `src/mynotes/pptx_importer.py`, `src/mynotes/exports.py`, `src/mynotes/backup.py`.
- `src/mynotes/provider.py`, `src/mynotes/realtime.py`, `src/mynotes/diarization.py`.
- `src/mynotes/agent_harness.py`, `src/mynotes/bridge.py`, `src/mynotes/agent_files.py`, `src/mynotes/security.py`.
