import React, { useState, useRef, useCallback } from 'react';
import './UploadPage.css';

interface Props { onJobStarted: (jobId: string) => void; }

const API_URL = process.env.REACT_APP_API_URL || '';

const ACCEPTED = ['image/jpeg', 'image/png', 'image/webp'];

export default function UploadPage({ onJobStarted }: Props) {
    const [files, setFiles] = useState<File[]>([]);
    const [previews, setPreviews] = useState<string[]>([]);
    const [dragging, setDragging] = useState(false);
    const [uploading, setUploading] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [propertyName, setPropertyName] = useState('');
    const inputRef = useRef<HTMLInputElement>(null);

    const addFiles = useCallback((incoming: File[]) => {
        const valid = incoming.filter(f => ACCEPTED.includes(f.type));
        if (valid.length !== incoming.length) {
            setError('Some files were skipped — only JPEG, PNG, and WEBP are supported.');
        }
        setFiles(prev => {
            const combined = [...prev, ...valid].slice(0, 20); // max 20 images
            // Generate previews
            combined.forEach((f, i) => {
                if (!previews[i]) {
                    const reader = new FileReader();
                    reader.onload = e => {
                        setPreviews(p => { const n = [...p]; n[i] = e.target?.result as string; return n; });
                    };
                    reader.readAsDataURL(f);
                }
            });
            return combined;
        });
    }, [previews]);

    const handleDrop = (e: React.DragEvent) => {
        e.preventDefault();
        setDragging(false);
        addFiles(Array.from(e.dataTransfer.files));
    };

    const removeFile = (i: number) => {
        setFiles(f => f.filter((_, idx) => idx !== i));
        setPreviews(p => p.filter((_, idx) => idx !== i));
    };

    const handleGenerate = async () => {
        if (files.length < 2) { setError('Upload at least 2 photos to generate a video.'); return; }
        setError(null);
        setUploading(true);
        try {
            // Step 1: Create job and get presigned upload URLs
            const res = await fetch(`${API_URL}/jobs`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    property_name: propertyName,
                    filenames: files.map(f => f.name),
                }),
            });
            if (!res.ok) {
                const err = await res.json().catch(() => ({}));
                throw new Error(err.error || `Server error ${res.status}`);
            }
            const data = await res.json();
            const { job_id, upload_urls } = data;

            // Step 2: Upload each image directly to S3 via presigned URL
            await Promise.all(
                files.map((file, i) =>
                    fetch(upload_urls[i].upload_url, {
                        method: 'PUT',
                        body: file,
                        headers: { 'Content-Type': 'application/octet-stream' },
                    })
                )
            );

            // Step 3: Tell the API all uploads are done → triggers pipeline
            await fetch(`${API_URL}/jobs/${job_id}/start`, { method: 'POST' });

            onJobStarted(job_id);
        } catch (e: any) {
            setError(e.message || 'Failed to start job');
            setUploading(false);
        }
    };

    return (
        <div className="upload-page">
            {/* Property name */}
            <div className="property-name-row">
                <input
                    className="property-input"
                    placeholder="Property name (optional) — e.g. 123 Oak Street"
                    value={propertyName}
                    onChange={e => setPropertyName(e.target.value)}
                />
            </div>

            {/* Drop zone */}
            <div
                className={`dropzone ${dragging ? 'dragging' : ''} ${files.length > 0 ? 'has-files' : ''}`}
                onDrop={handleDrop}
                onDragOver={e => { e.preventDefault(); setDragging(true); }}
                onDragLeave={() => setDragging(false)}
                onClick={() => files.length === 0 && inputRef.current?.click()}
            >
                {files.length === 0 ? (
                    <div className="drop-hint">
                        <span className="drop-icon">📸</span>
                        <p>Drop property photos here</p>
                        <p className="drop-sub">or click to browse · JPEG, PNG, WEBP · up to 20 images</p>
                        <button className="browse-btn" onClick={e => { e.stopPropagation(); inputRef.current?.click(); }}>
                            Browse Files
                        </button>
                    </div>
                ) : (
                    <div className="preview-grid">
                        {files.map((f, i) => (
                            <div key={i} className="preview-item">
                                <img src={previews[i] || ''} alt={f.name} />
                                <button className="remove-btn" onClick={e => { e.stopPropagation(); removeFile(i); }}>✕</button>
                                <div className="preview-name">{f.name.split('.')[0].slice(0, 20)}</div>
                            </div>
                        ))}
                        <div className="add-more" onClick={e => { e.stopPropagation(); inputRef.current?.click(); }}>
                            <span>+</span>
                            <span>Add more</span>
                        </div>
                    </div>
                )}
            </div>
            <input ref={inputRef} type="file" accept="image/jpeg,image/png,image/webp" multiple hidden
                onChange={e => e.target.files && addFiles(Array.from(e.target.files))} />

            {/* Stats + generate */}
            {files.length > 0 && (
                <div className="generate-row">
                    <div className="file-stats">
                        <span>{files.length} photo{files.length > 1 ? 's' : ''} selected</span>
                        <span className="dot">·</span>
                        <span>~{Math.round(files.reduce((a, f) => a + f.size, 0) / 1024 / 1024 * 10) / 10} MB</span>
                        <span className="dot">·</span>
                        <span>Est. {Math.ceil(files.length * 8 / 60)} min to generate</span>
                    </div>
                    <button
                        className="generate-btn"
                        onClick={handleGenerate}
                        disabled={uploading || files.length < 2}
                    >
                        {uploading ? (
                            <><span className="spinner" /> Uploading...</>
                        ) : (
                            <>🎬 Generate Video</>
                        )}
                    </button>
                </div>
            )}

            {error && <div className="error-msg">⚠️ {error}</div>}

            {/* How it works */}
            <div className="how-it-works">
                <h3>How it works</h3>
                <div className="steps">
                    {[
                        { icon: '🔍', label: 'Analyze', desc: 'Claude examines each photo and extracts room type, style, and selling points' },
                        { icon: '✍️', label: 'Story', desc: 'Claude sequences the photos and writes a 60-90 second narrative script' },
                        { icon: '🎙️', label: 'Voiceover', desc: 'Amazon Polly converts the script to professional narration audio' },
                        { icon: '🎬', label: 'Video', desc: 'Nova Reel generates cinematic video clips from each photo' },
                        { icon: '🎞️', label: 'Assemble', desc: 'Clips and audio are merged into a final MP4 ready for social media' },
                    ].map(s => (
                        <div key={s.label} className="step">
                            <div className="step-icon">{s.icon}</div>
                            <div className="step-label">{s.label}</div>
                            <div className="step-desc">{s.desc}</div>
                        </div>
                    ))}
                </div>
            </div>
        </div>
    );
}
