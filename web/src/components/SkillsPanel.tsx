import { FormEvent, useCallback, useEffect, useRef, useState } from 'react';

export type SkillItem = {
  id: number;
  slug: string;
  name: string;
  description: string;
  triggers: string;
  body: string;
  enabled: boolean;
  created_at: string;
  updated_at: string;
  use_count: number;
  last_used_at: string | null;
};

type SkillsResponse = {
  items: SkillItem[];
  count: number;
};

export type SkillsPanelProps = {
  token: string;
  onUseInChat?: (slug: string) => void;
};

export function SkillsPanel({ token, onUseInChat }: SkillsPanelProps) {
  const [disabled, setDisabled] = useState(false);
  const disabledRef = useRef(false);
  const [skills, setSkills] = useState<SkillItem[]>([]);
  const [query, setQuery] = useState('');
  const [error, setError] = useState('');

  // Create / Edit modal state
  const [isEditing, setIsEditing] = useState(false);
  const [editingSlug, setEditingSlug] = useState<string | null>(null);
  const [name, setName] = useState('');
  const [slug, setSlug] = useState('');
  const [description, setDescription] = useState('');
  const [triggers, setTriggers] = useState('');
  const [body, setBody] = useState('');
  const [formBusy, setFormBusy] = useState(false);
  const [formError, setFormError] = useState('');

  // Import modal state
  const [isImporting, setIsImporting] = useState(false);
  const [importMarkdown, setImportMarkdown] = useState('');
  const [importBusy, setImportBusy] = useState(false);
  const [importError, setImportError] = useState('');

  // Delete confirmation
  const [confirmDeleteSlug, setConfirmDeleteSlug] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);
  const fetchGen = useRef(0);

  // Non-blocking export toast
  const [toastMessage, setToastMessage] = useState<string | null>(null);
  const toastTimeoutRef = useRef<number | null>(null);

  useEffect(() => {
    return () => {
      if (toastTimeoutRef.current) window.clearTimeout(toastTimeoutRef.current);
    };
  }, []);

  const load = useCallback(async () => {
    if (disabledRef.current || !token) return;
    const gen = ++fetchGen.current;
    try {
      const res = await fetch('/api/skills', {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (res.status === 409) {
        disabledRef.current = true;
        setDisabled(true);
        return;
      }
      if (!res.ok) {
        const errBody = await res.json().catch(() => ({}));
        throw new Error(errBody.error || `Failed to load skills (${res.status})`);
      }
      const data = (await res.json()) as SkillsResponse;
      if (gen !== fetchGen.current) return;
      setSkills(data.items || []);
      setError('');
    } catch (err) {
      if (gen !== fetchGen.current) return;
      setError(err instanceof Error ? err.message : 'Failed to load skills');
    }
  }, [token]);

  useEffect(() => {
    void load();
  }, [load]);

  const openCreate = () => {
    setIsEditing(true);
    setEditingSlug(null);
    setName('');
    setSlug('');
    setDescription('');
    setTriggers('');
    setBody('');
    setFormError('');
  };

  const openEdit = (skill: SkillItem) => {
    setIsEditing(true);
    setEditingSlug(skill.slug);
    setName(skill.name);
    setSlug(skill.slug);
    setDescription(skill.description);
    setTriggers(skill.triggers);
    setBody(skill.body);
    setFormError('');
  };

  const closeForm = () => {
    setIsEditing(false);
    setEditingSlug(null);
    setFormError('');
  };

  const handleSave = async (e: FormEvent) => {
    e.preventDefault();
    setFormBusy(true);
    setFormError('');
    try {
      if (editingSlug) {
        // Update existing skill
        const res = await fetch(`/api/skills/${encodeURIComponent(editingSlug)}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
          body: JSON.stringify({ name, description, triggers, body }),
        });
        if (!res.ok) {
          const errBody = await res.json().catch(() => ({}));
          throw new Error(errBody.error || `Failed to update skill (${res.status})`);
        }
      } else {
        // Create new skill
        const payload: Record<string, unknown> = { name, description, triggers, body };
        if (slug.trim()) payload.slug = slug.trim();
        const res = await fetch('/api/skills', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
          body: JSON.stringify(payload),
        });
        if (!res.ok) {
          const errBody = await res.json().catch(() => ({}));
          throw new Error(errBody.error || `Failed to create skill (${res.status})`);
        }
      }
      closeForm();
      await load();
    } catch (err) {
      setFormError(err instanceof Error ? err.message : 'Failed to save skill');
    } finally {
      setFormBusy(false);
    }
  };

  const handleToggleEnabled = async (skill: SkillItem) => {
    try {
      const res = await fetch(`/api/skills/${encodeURIComponent(skill.slug)}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
        body: JSON.stringify({ enabled: !skill.enabled }),
      });
      if (!res.ok) {
        const errBody = await res.json().catch(() => ({}));
        throw new Error(errBody.error || `Failed to update (${res.status})`);
      }
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to toggle skill');
    }
  };

  const handleDelete = async (slugToDelete: string) => {
    setDeleting(true);
    try {
      const res = await fetch(`/api/skills/${encodeURIComponent(slugToDelete)}`, {
        method: 'DELETE',
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!res.ok && res.status !== 404) {
        const errBody = await res.json().catch(() => ({}));
        throw new Error(errBody.error || `Failed to delete (${res.status})`);
      }
      setConfirmDeleteSlug(null);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete skill');
    } finally {
      setDeleting(false);
    }
  };

  const handleImport = async (e: FormEvent) => {
    e.preventDefault();
    const md = importMarkdown.trim();
    if (!md) return;
    setImportBusy(true);
    setImportError('');
    try {
      const res = await fetch('/api/skills', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
        body: JSON.stringify({ markdown: md }),
      });
      if (!res.ok) {
        const errBody = await res.json().catch(() => ({}));
        throw new Error(errBody.error || `Failed to import skill (${res.status})`);
      }
      setIsImporting(false);
      setImportMarkdown('');
      await load();
    } catch (err) {
      setImportError(err instanceof Error ? err.message : 'Failed to import skill');
    } finally {
      setImportBusy(false);
    }
  };

  const handleExport = (skill: SkillItem) => {
    const md = [
      '---',
      `slug: ${skill.slug}`,
      `name: ${skill.name}`,
      `description: ${skill.description}`,
      `triggers: ${skill.triggers}`,
      '---',
      skill.body,
    ].join('\n');
    const blob = new Blob([md], { type: 'text/markdown;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${skill.slug}.md`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);

    if (toastTimeoutRef.current) {
      window.clearTimeout(toastTimeoutRef.current);
    }
    setToastMessage(`Exported "${skill.name}" as ${skill.slug}.md — check your browser's Downloads folder.`);
    toastTimeoutRef.current = window.setTimeout(() => {
      setToastMessage(null);
    }, 6000);
  };

  if (disabled) {
    return (
      <div className="stream-body" style={{ padding: '24px 20px' }}>
        <div className="job-notice" style={{ maxWidth: 640 }}>
          <strong>Skills library is disabled</strong>
          <span>Set <code>CONVEYOR_SKILLS_ENABLED=true</code> on the server to enable skills.</span>
        </div>
      </div>
    );
  }

  const filtered = skills.filter((item) => {
    if (!query.trim()) return true;
    const q = query.toLowerCase();
    return (
      item.name.toLowerCase().includes(q) ||
      item.slug.toLowerCase().includes(q) ||
      item.description.toLowerCase().includes(q) ||
      item.triggers.toLowerCase().includes(q) ||
      item.body.toLowerCase().includes(q)
    );
  });

  return (
    <div className="stream-body" style={{ display: 'flex', flexDirection: 'column', height: '100%', overflowY: 'auto', padding: 16, gap: 16 }}>
      {toastMessage && (
        <div
          role="status"
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            gap: 12,
            padding: '10px 14px',
            borderRadius: 8,
            border: '1px solid #bbf7d0',
            background: '#f0fdf4',
            color: '#166534',
            fontSize: '0.85rem',
            lineHeight: 1.4,
          }}
        >
          <span>{toastMessage}</span>
          <button
            type="button"
            aria-label="Dismiss notice"
            onClick={() => {
              if (toastTimeoutRef.current) window.clearTimeout(toastTimeoutRef.current);
              setToastMessage(null);
            }}
            style={{
              background: 'transparent',
              border: 0,
              color: '#166534',
              cursor: 'pointer',
              fontSize: '18px',
              padding: '0 4px',
              lineHeight: 1,
            }}
          >
            ×
          </button>
        </div>
      )}

      {error && (
        <div className="error-banner global" style={{ margin: 0 }}>
          {error}
          <button type="button" onClick={() => setError('')}>×</button>
        </div>
      )}

      {/* Top Banner / Actions */}
      <section className="memory-card">
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: 8 }}>
          <h3 style={{ margin: 0, fontSize: '0.95rem', fontWeight: 600 }}>
            Skills library · {skills.length} saved procedure{skills.length === 1 ? '' : 's'}
          </h3>
          <div style={{ display: 'flex', gap: 8 }}>
            <button type="button" className="action-btn" onClick={openCreate}>
              + Create skill
            </button>
            <button type="button" className="action-btn" onClick={() => { setIsImporting(!isImporting); setImportError(''); }}>
              {isImporting ? 'Cancel import' : 'Import Markdown'}
            </button>
          </div>
        </div>
        <p className="memory-secondary" style={{ fontSize: '0.8rem', margin: '8px 0 0' }}>
          Reusable operator-authored procedures. Loaded into chat context via <code>/skill &lt;slug&gt;</code> or on demand by the assistant. Skills never grant extra permissions or bypass approvals.
        </p>
      </section>

      {/* Import Markdown Drawer */}
      {isImporting && (
        <section className="memory-card" style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
          <h4 style={{ margin: 0, fontSize: '0.9rem', fontWeight: 600 }}>Import Skill from Markdown</h4>
          <p className="memory-secondary" style={{ fontSize: '0.78rem', margin: 0 }}>
            Paste Markdown with front-matter (<code>--- name / description / triggers --- body</code>).
          </p>
          <form onSubmit={handleImport} style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            <textarea
              className="memory-input"
              rows={6}
              placeholder={`---\nname: Deploy Checklist\ndescription: Standard steps for production deployment\ntriggers: deploy, release\n---\n1. Run tests\n2. Check migrations\n3. Deploy to prod`}
              value={importMarkdown}
              onChange={(e) => setImportMarkdown(e.target.value)}
              aria-label="Markdown to import"
              style={{ fontFamily: 'monospace', fontSize: '0.85rem' }}
            />
            {importError && <div className="memory-error">{importError}</div>}
            <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
              <button type="button" className="action-btn" onClick={() => setIsImporting(false)} disabled={importBusy}>
                Cancel
              </button>
              <button type="submit" className="action-btn" disabled={importBusy || !importMarkdown.trim()}>
                {importBusy ? 'Importing…' : 'Import'}
              </button>
            </div>
          </form>
        </section>
      )}

      {/* Create / Edit Form Modal/Drawer */}
      {isEditing && (
        <section className="memory-card" style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          <h4 style={{ margin: 0, fontSize: '0.92rem', fontWeight: 600 }}>
            {editingSlug ? `Edit skill: ${editingSlug}` : 'Create new skill'}
          </h4>
          <form onSubmit={handleSave} style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
            <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
              <div style={{ flex: 2, minWidth: 200, display: 'flex', flexDirection: 'column', gap: 4 }}>
                <label style={{ fontSize: '0.78rem', fontWeight: 600 }}>
                  Name * <span className="memory-secondary">(1-80 chars)</span>
                </label>
                <input
                  type="text"
                  className="memory-input"
                  maxLength={80}
                  placeholder="e.g. Code Review Checklist"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  required
                />
              </div>
              {!editingSlug && (
                <div style={{ flex: 1, minWidth: 160, display: 'flex', flexDirection: 'column', gap: 4 }}>
                  <label style={{ fontSize: '0.78rem', fontWeight: 600 }}>
                    Slug <span className="memory-secondary">(optional, auto-derived)</span>
                  </label>
                  <input
                    type="text"
                    className="memory-input"
                    maxLength={48}
                    placeholder="e.g. code-review"
                    value={slug}
                    onChange={(e) => setSlug(e.target.value)}
                  />
                </div>
              )}
            </div>

            <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
              <label style={{ fontSize: '0.78rem', fontWeight: 600 }}>
                Description * <span className="memory-secondary">(1-300 chars, visible in assistant index)</span>
              </label>
              <input
                type="text"
                className="memory-input"
                maxLength={300}
                placeholder="What this procedure does and when to apply it"
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                required
              />
            </div>

            <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
              <label style={{ fontSize: '0.78rem', fontWeight: 600 }}>
                Triggers <span className="memory-secondary">(optional, comma-separated keywords)</span>
              </label>
              <input
                type="text"
                className="memory-input"
                maxLength={200}
                placeholder="e.g. review, pr, pull request"
                value={triggers}
                onChange={(e) => setTriggers(e.target.value)}
              />
            </div>

            <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <label style={{ fontSize: '0.78rem', fontWeight: 600 }}>
                  Procedure Body * <span className="memory-secondary">(Markdown instructions, steps, rules)</span>
                </label>
                <span className="memory-secondary" style={{ fontSize: '0.75rem' }}>
                  {body.length} / 8000
                </span>
              </div>
              <textarea
                className="memory-input"
                rows={10}
                maxLength={8000}
                placeholder="Write the full instructions for the assistant..."
                value={body}
                onChange={(e) => setBody(e.target.value)}
                required
                style={{ fontFamily: 'monospace', fontSize: '0.85rem' }}
              />
            </div>

            {formError && <div className="memory-error">{formError}</div>}

            <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end', marginTop: 4 }}>
              <button type="button" className="action-btn" onClick={closeForm} disabled={formBusy}>
                Cancel
              </button>
              <button type="submit" className="action-btn" disabled={formBusy || !name.trim() || !description.trim() || !body.trim()}>
                {formBusy ? 'Saving…' : 'Save skill'}
              </button>
            </div>
          </form>
        </section>
      )}

      {/* Filter / Search Bar */}
      <section className="memory-card" style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
        <input
          type="search"
          className="memory-input"
          value={query}
          placeholder="Search skills by name, slug, description, triggers, or content…"
          onChange={(e) => setQuery(e.target.value)}
          style={{ flex: 1 }}
          aria-label="Search skills"
        />
        {query && (
          <button type="button" className="action-btn" onClick={() => setQuery('')}>
            Clear
          </button>
        )}
      </section>

      {/* Skills List */}
      <section style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
        {filtered.length === 0 ? (
          <p className="memory-secondary" style={{ fontSize: '0.85rem' }}>
            {query ? `No skills match “${query}”.` : 'No skills in library yet. Click "+ Create skill" or "Import Markdown" to add one.'}
          </p>
        ) : (
          filtered.map((skill) => (
            <article key={skill.slug} className="memory-card" style={{ padding: 14 }}>
              <header style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: 12, marginBottom: 8, flexWrap: 'wrap' }}>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                    <strong style={{ fontSize: '0.98rem' }}>{skill.name}</strong>
                    <code style={{ fontSize: '0.78rem', background: 'var(--bg-subtle, rgba(0,0,0,0.05))', padding: '1px 6px', borderRadius: 4 }}>
                      {skill.slug}
                    </code>
                    <button
                      type="button"
                      className={`status-badge ${skill.enabled ? 'completed' : 'interrupted'}`}
                      style={{ padding: '2px 8px', border: 'none', cursor: 'pointer', fontSize: '0.72rem' }}
                      title="Click to toggle enabled/disabled"
                      onClick={() => void handleToggleEnabled(skill)}
                    >
                      {skill.enabled ? '● Enabled' : '○ Disabled'}
                    </button>
                  </div>
                  <div className="memory-secondary" style={{ fontSize: '0.76rem' }}>
                    Used {skill.use_count} time{skill.use_count === 1 ? '' : 's'}
                    {skill.last_used_at ? ` · Last used ${new Date(skill.last_used_at).toLocaleDateString()}` : ' · Never used'}
                  </div>
                </div>

                <div style={{ display: 'flex', gap: 6, alignItems: 'center', flexWrap: 'wrap' }}>
                  {onUseInChat && (
                    <button
                      type="button"
                      className="action-btn"
                      style={{ fontSize: '0.8rem', padding: '3px 10px' }}
                      onClick={() => onUseInChat(skill.slug)}
                      title="Switch to chat and prefill /skill"
                    >
                      Use in chat
                    </button>
                  )}
                  <button
                    type="button"
                    className="action-btn"
                    style={{ fontSize: '0.8rem', padding: '3px 10px' }}
                    onClick={() => openEdit(skill)}
                  >
                    Edit
                  </button>
                  <button
                    type="button"
                    className="action-btn"
                    style={{ fontSize: '0.8rem', padding: '3px 10px' }}
                    onClick={() => handleExport(skill)}
                  >
                    Export
                  </button>
                  {confirmDeleteSlug === skill.slug ? (
                    <div style={{ display: 'flex', gap: 4, alignItems: 'center' }}>
                      <span className="memory-confirm-warning" style={{ fontSize: '0.76rem' }}>Delete?</span>
                      <button
                        type="button"
                        className="action-btn memory-btn-danger"
                        style={{ fontSize: '0.8rem', padding: '3px 8px' }}
                        disabled={deleting}
                        onClick={() => void handleDelete(skill.slug)}
                      >
                        {deleting ? 'Deleting…' : 'Confirm delete'}
                      </button>
                      <button
                        type="button"
                        className="action-btn"
                        style={{ fontSize: '0.8rem', padding: '3px 8px' }}
                        disabled={deleting}
                        onClick={() => setConfirmDeleteSlug(null)}
                      >
                        Cancel
                      </button>
                    </div>
                  ) : (
                    <button
                      type="button"
                      className="action-btn"
                      style={{ fontSize: '0.8rem', padding: '3px 10px' }}
                      onClick={() => setConfirmDeleteSlug(skill.slug)}
                    >
                      Delete
                    </button>
                  )}
                </div>
              </header>

              <div style={{ fontSize: '0.85rem', marginBottom: 6, color: 'var(--text-main, #222)' }}>
                {skill.description}
              </div>

              {skill.triggers && (
                <div className="memory-secondary" style={{ fontSize: '0.75rem', marginBottom: 8 }}>
                  <strong>Triggers:</strong> {skill.triggers}
                </div>
              )}

              <details style={{ marginTop: 6, fontSize: '0.82rem' }}>
                <summary style={{ cursor: 'pointer', color: 'var(--text-muted, #666)' }}>
                  View procedure ({skill.body.length} chars) ⌄
                </summary>
                <pre style={{
                  background: 'var(--bg-subtle, rgba(0,0,0,0.03))',
                  padding: 10,
                  borderRadius: 6,
                  marginTop: 6,
                  whiteSpace: 'pre-wrap',
                  wordBreak: 'break-word',
                  fontFamily: 'monospace',
                  fontSize: '0.8rem',
                  maxHeight: 250,
                  overflowY: 'auto',
                }}>
                  {skill.body}
                </pre>
              </details>
            </article>
          ))
        )}
      </section>
    </div>
  );
}
