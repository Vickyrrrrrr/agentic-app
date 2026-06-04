import React, { useEffect, useRef, useState } from 'react';
import mermaid from 'mermaid';
import Editor from '@monaco-editor/react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { FileText, Code2 } from 'lucide-react';

// Initialize mermaid with dark theme matching the IDE
mermaid.initialize({
  startOnLoad: false,
  theme: 'dark',
  fontFamily: 'Inter, sans-serif',
  securityLevel: 'loose',
});

// For WaveDrom, we need to load it globally or use a react wrapper if available.
// The package.json has 'wavedrom', which usually exposes window.WaveDrom.
declare global {
  interface Window {
    WaveDrom: any;
  }
}

interface DiagramViewerProps {
  filename: string;
  content: string;
  language: string;
}

export const DiagramViewer: React.FC<DiagramViewerProps> = ({ filename, content, language }) => {
  const mermaidRef = useRef<HTMLDivElement>(null);
  const wavedromRef = useRef<HTMLDivElement>(null);
  const [error, setError] = useState<string | null>(null);
  const [mdViewMode, setMdViewMode] = useState<'preview' | 'code'>('preview');

  const isSvg = filename.endsWith('.svg');
  const isMermaid = filename.endsWith('.mermaid');
  const isWaveDrom = filename.endsWith('.json') && content.includes('signal');
  const isMarkdown = filename.endsWith('.md');
  const isDiagram = isSvg || isMermaid || isWaveDrom;

  useEffect(() => {
    setError(null);
    if (!content) return;

    if (isMermaid && mermaidRef.current) {
      mermaidRef.current.innerHTML = '';
      
      // Clean up the content in case the AI added markdown code fences (```mermaid ... ```)
      let cleanContent = content.trim();
      if (cleanContent.startsWith('```mermaid')) {
        cleanContent = cleanContent.replace(/^```mermaid\n?/, '').replace(/\n?```$/, '');
      } else if (cleanContent.startsWith('```')) {
        cleanContent = cleanContent.replace(/^```[a-z]*\n?/, '').replace(/\n?```$/, '');
      }
      
      mermaid.render('mermaid-svg-' + Date.now(), cleanContent)
        .then((result) => {
          if (mermaidRef.current) {
            mermaidRef.current.innerHTML = result.svg;
          }
        })
        .catch((err) => {
          console.error('Mermaid render error:', err);
          setError(err.message || 'Failed to render Mermaid diagram.');
        });
    }

    if (isWaveDrom && wavedromRef.current) {
      try {
        const parsed = JSON.parse(content);
        // We inject a script tag or execute window.WaveDrom if available
        // Simple fallback since wavedrom might need to be imported
        if (window.WaveDrom) {
          wavedromRef.current.id = 'wavedrom-' + Date.now();
          window.WaveDrom.RenderWaveForm(wavedromRef.current.id, parsed, 'wavedrom');
        } else {
          setError('WaveDrom library not loaded. Ensure wavedrom script is included.');
        }
      } catch (err: any) {
        setError('Invalid JSON for WaveDrom: ' + err.message);
      }
    }
  }, [content, isMermaid, isWaveDrom]);

  // SVG Rendering
  if (isSvg) {
    return (
      <div style={{ padding: '2rem', display: 'flex', justifyContent: 'center', height: '100%', overflow: 'auto', background: 'var(--bg)' }}>
        <div dangerouslySetInnerHTML={{ __html: content }} />
      </div>
    );
  }

  // Mermaid Rendering
  if (isMermaid) {
    return (
      <div style={{ height: '100%', overflow: 'auto', padding: '2rem', background: 'var(--bg)' }}>
        {error ? (
          <div style={{ color: 'var(--fail)' }}>{error}</div>
        ) : (
          <div ref={mermaidRef} style={{ display: 'flex', justifyContent: 'center' }} />
        )}
      </div>
    );
  }

  // WaveDrom Rendering
  if (isWaveDrom) {
    return (
      <div style={{ height: '100%', overflow: 'auto', padding: '2rem', background: 'var(--bg)' }}>
        {error ? (
          <div style={{ color: 'var(--fail)' }}>{error}</div>
        ) : (
          <div ref={wavedromRef} style={{ display: 'flex', justifyContent: 'center' }} />
        )}
      </div>
    );
  }

  // Markdown Rendering (with toggle)
  if (isMarkdown && mdViewMode === 'preview') {
    return (
      <div style={{ height: '100%', display: 'flex', flexDirection: 'column', position: 'relative' }}>
        <button
          onClick={() => setMdViewMode('code')}
          style={{
            position: 'absolute', top: '10px', right: '15px', zIndex: 10,
            background: 'var(--bg)', border: '1px solid var(--border)', color: 'var(--text-secondary)',
            borderRadius: '4px', padding: '6px', cursor: 'pointer', display: 'flex', alignItems: 'center', gap: '6px', fontSize: '12px'
          }}
          title="View raw code"
        >
          <Code2 size={14} /> Raw
        </button>
        <div className="adoc-prose" style={{ padding: '2rem', flex: 1, overflow: 'auto', background: 'var(--bg)', color: 'var(--text-primary)' }}>
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{content}</ReactMarkdown>
        </div>
      </div>
    );
  }

  // Fallback to Monaco Editor (for code or raw Markdown)
  return (
    <div style={{ height: '100%', position: 'relative' }}>
      {isMarkdown && (
        <button
          onClick={() => setMdViewMode('preview')}
          style={{
            position: 'absolute', top: '10px', right: '25px', zIndex: 10,
            background: 'var(--bg)', border: '1px solid var(--border)', color: 'var(--text-secondary)',
            borderRadius: '4px', padding: '6px', cursor: 'pointer', display: 'flex', alignItems: 'center', gap: '6px', fontSize: '12px'
          }}
          title="Preview markdown"
        >
          <FileText size={14} /> Preview
        </button>
      )}
      <Editor
        height="100%"
        language={language}
        theme="vs-dark"
        value={content || 'Loading preview...'}
        options={{
          readOnly: true,
          minimap: { enabled: false },
          fontSize: 13,
          fontFamily: "'Geist Mono', 'Fira Code', monospace",
          scrollBeyondLastLine: false,
          smoothScrolling: true,
          wordWrap: 'on',
          padding: { top: 12, bottom: 12 },
        }}
      />
    </div>
  );
};
