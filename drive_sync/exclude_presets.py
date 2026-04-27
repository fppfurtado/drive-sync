"""Padrões padrão de exclusão para o modo `bisync` em pastas de código.

São aplicados *em adição* aos padrões que o usuário declarou em `exclude`
no config.yaml. A ideia é cobrir os "lixos" universais de qualquer árvore
de código (build outputs, caches, ambientes virtuais), poupando o usuário
de listar tudo manualmente para cada tarefa.

Quem quiser desligar isso pode usar `auto_exclude: false` na tarefa.
"""
from __future__ import annotations

# Padrões no formato glob usado pelo rclone (`--exclude`).
# Mantidos em listas separadas por categoria — facilita auditoria e PRs.

VCS = [
    # NÃO excluímos .git/ — em modo bisync é importante ele ir junto,
    # senão a "cópia" na nuvem não é um repo Git utilizável.
    # Mas excluímos arquivos transitórios *dentro* dele:
    ".git/objects/info/**",
    ".git/logs/**",
    ".git/lfs/tmp/**",
    ".git/COMMIT_EDITMSG",
    ".git/MERGE_*",
    ".git/index.lock",
    ".git/HEAD.lock",
]

PYTHON = [
    "__pycache__/**",
    "*.pyc",
    "*.pyo",
    ".venv/**",
    "venv/**",
    "env/**",
    ".tox/**",
    ".pytest_cache/**",
    ".mypy_cache/**",
    ".ruff_cache/**",
    "*.egg-info/**",
    "build/**",
    "dist/**",
]

JS_TS = [
    "node_modules/**",
    ".next/**",
    ".nuxt/**",
    ".turbo/**",
    ".svelte-kit/**",
    ".parcel-cache/**",
    ".vite/**",
    "dist/**",
    "out/**",
    ".pnpm-store/**",
    "yarn-error.log",
]

RUST = [
    "target/**",
    "Cargo.lock.bak",
]

GO = [
    "vendor/**",  # opcional — mantido pois é regenerável via `go mod vendor`
]

JAVA_KOTLIN = [
    "build/**",
    ".gradle/**",
    "out/**",
    "*.class",
]

EDITOR = [
    ".idea/**",
    ".vscode/**",
    "*.swp",
    "*.swo",
    "*~",
    ".DS_Store",
]

GENERAL = [
    "*.log",
    "*.tmp",
    "*.bak",
    "core",  # core dumps
]


def default_excludes_for_code() -> list[str]:
    """Lista consolidada que o `bisync` aplica em pastas com código."""
    return [
        *VCS,
        *PYTHON,
        *JS_TS,
        *RUST,
        *GO,
        *JAVA_KOTLIN,
        *EDITOR,
        *GENERAL,
    ]
