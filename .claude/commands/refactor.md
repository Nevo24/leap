# Comprehensive Codebase Refactor

Perform a comprehensive codebase refactor, bug-fix sweep, and documentation sync.

## 1. Code Quality & Architecture

### Decomposition
- Identify functions exceeding **50 lines** or files exceeding **300 lines**
- Refactor these into smaller, modular components
- Extract repeated logic into reusable functions/modules

### Cleanup
- Remove dead code (unused functions, commented-out code blocks)
- Remove unused imports
- Identify and remove orphaned files

### DRY & Typing
- Eliminate logic duplication across the codebase
- Ensure **100% Type Hinting coverage** (optimized for PyCharm)
- Add type hints to all function signatures, variables, and return types
- Use `typing` module annotations where appropriate

## 2. Bug Fixes

### Critical Fixes
- If you encounter definitive bugs or logic errors, fix them immediately
- Document each fix with a clear comment explaining what was wrong

### The Certainty Rule ⚠️
- **ONLY** apply fixes if you are absolutely certain the current logic is incorrect
- If a section of code is ambiguous or unclear:
  - **DO NOT change it**
  - Instead, list it as a "Potential Issue" in your summary
  - Flag it with a comment: `# REVIEW: [description of concern]`

## 3. Documentation (The "Short vs. Deep" Rule)

### README.md
- Update with new/changed functionality
- Keep it **short, concise, and professional** for GitHub visitors
- Focus on what changed and how to use it
- Target audience: external users discovering the project

### CLAUDE.md
- Update with a **deep-dive technical explanation** of changes
- Explain architectural decisions and implementation details
- Target audience: future AI agents working on the project
- Include:
  - What was changed and why
  - Technical rationale behind refactoring decisions
  - New patterns or conventions introduced

## 4. Constraints

### Functionality
- **Ensure all refactors maintain existing functionality**
- If changing behavior, document it clearly in both README and CLAUDE.md
- Consider backward compatibility

### Naming Conventions
- **Maintain consistent snake_case** for Python:
  - Functions: `process_message()`
  - Variables: `queue_manager`
  - Files: `socket_handler.py`
- Class names: `PascalCase` (e.g., `LeapServer`)
- Constants: `UPPER_SNAKE_CASE` (e.g., `QUEUE_DIR`)

### Code Style
- Follow PEP 8 guidelines
- Maximum line length: 100 characters (PyCharm default)
- Use meaningful variable names (avoid single letters except in short loops)

## Workflow

1. **Scan the codebase** — identify targets for refactoring
2. **Prioritize by impact** — start with the largest/most complex files
3. **Refactor incrementally** — one module at a time
4. **Test after each change** — ensure nothing breaks
5. **Update documentation** — sync README.md and CLAUDE.md
6. **Provide summary** — list all changes and any "Potential Issues" flagged

## Summary Template

After completing the refactor, provide a summary in this format:

```markdown
## Refactor Summary

### Files Changed
- `path/to/file.py` - [Brief description of changes]

### Refactoring Done
- Decomposed large functions: [list functions]
- Removed dead code: [list removed items]
- Fixed bugs: [list fixes with brief explanation]
- Added type hints: [list modules with new type coverage]

### Potential Issues (Review Required)
- `path/to/file.py:123` - [Description of ambiguous code that needs human review]

### Documentation Updates
- README.md: [summary of changes]
- CLAUDE.md: [summary of changes]
```

## Important Notes

- **Ask before making large architectural changes** — if refactoring requires significant restructuring, confirm with the user first
- **Preserve commit-ability** — ensure the codebase remains in a working state at all times
