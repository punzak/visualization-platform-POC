import React, { useEffect, useState, useRef } from 'react';
import './JobStatus.css';

interface Props { jobId: string; onReset: () => void; }

interface Segment {
    segment_index: number;
    room_type?: string;
    script_text?: string;
    video_prompt?: string;
    camera_movement?: string;
    status?: string;
    video_s3_url?: string;
    thumbnail_url?: string;
}

interface JobData {
    job_id: string;
    status: string;
    property_name?: string;
    image_count?: number;
    images_analyzed?: number;
    created_at?: string;
    full_script?: string;
    voiceover_url?: string;
    final_video_url?: string;
    segments?: Segment[];
    error?: string;
}

const API_URL = process.env.REACT_APP_API_URL || '';

const STAGES = [
    { key: 'analyzing', label: 'Analyzing Photos', icon: '🔍', desc: 'Claude is examining each photo' },
    { key: 'sequencing', label: 'Writing Story', icon: '✍️', desc: 'Creating narrative and video prompts' },
    { key: 'voiceover', label: 'Generating Voiceover', icon: '🎙️', desc: 'Converting script to audio' },
    { key: 'generating', label: 'Generating Video', icon: '🎬', desc: 'Nova Reel is creating video clips' },
    { key: 'assembling', label: 'Assembling', icon: '🎞️', desc: 'Merging clips and audio' },
    { key: 'complete', label: 'Complete', icon: '✅', desc: 'Your video is ready' },
];

function stageIndex(status: string) {
    const idx = STAGES.findIndex(s => s.key === status);
    return idx === -1 ? 0 : idx;
}

export default function JobStatus({ jobId, onReset }: Props) {
    const [job, setJob] = useState<JobData | null>(null);
    const [error, setError] = useState<string | null>(null);
    const pollRef = useRef<NodeJS.Timeout | null>(null);

    const fetchJob = async () => {
        try {
            const res = await fetch(`${API_URL}/jobs/${jobId}`);
            if (!res.ok) throw new Error(`Status ${res.status}`);
            const data: JobData = await res.json();
            setJob(data);
            if (data.status === 'complete' || data.status === 'failed') {
                if (pollRef.current) clearInterval(pollRef.current);
            }
        } catch (e: any) {
            setError(e.message);
        }
    };

    useEffect(() => {
        fetchJob();
        pollRef.current = setInterval(fetchJob, 5000);
        return () => { if (pollRef.current) clearInterval(pollRef.current); };
    }, [jobId]);

    if (error) return (
        <div className="job-error">
            <p>⚠️ Could not load job status: {error}</p>
            <button onClick={fetchJob}>Retry</button>
        </div>
    );

    if (!job) return <div className="job-loading"><div className="spinner-lg" /><p>Loading...</p></div>;

    const currentStage = stageIndex(job.status);
    const isFailed = job.status === 'failed';
    const isDone = job.status === 'complete';

    return (
        <div className="job-status">
            {/* Header */}
            <div className="job-header">
                <div>
                    <div className="job-id">Job: {jobId.slice(0, 8)}...</div>
                    {job.property_name && <div className="job-property">{job.property_name}</div>}
                </div>
                <div className="job-meta">
                    {job.image_count && <span>{job.image_count} photos</span>}
                    {job.images_analyzed !== undefined && job.image_count && (
                        <span>{job.images_analyzed}/{job.image_count} analyzed</span>
                    )}
                </div>
            </div>

            {/* Stage progress */}
            <div className="stage-track">
                {STAGES.map((stage, i) => {
                    const done = i < currentStage || isDone;
                    const active = i === currentStage && !isDone && !isFailed;
                    const failed = isFailed && i === currentStage;
                    return (
                        <div key={stage.key} className={`stage ${done ? 'done' : ''} ${active ? 'active' : ''} ${failed ? 'failed' : ''}`}>
                            <div className="stage-icon">
                                {done ? '✓' : failed ? '✗' : active ? <span className="pulse">{stage.icon}</span> : stage.icon}
                            </div>
                            <div className="stage-label">{stage.label}</div>
                            {active && <div className="stage-desc">{stage.desc}</div>}
                        </div>
                    );
                })}
            </div>

            {/* Script preview */}
            {job.full_script && (
                <div className="script-section">
                    <div className="section-title">📝 Generated Script</div>
                    <div className="script-text">{job.full_script}</div>
                </div>
            )}

            {/* Segments */}
            {job.segments && job.segments.length > 0 && (
                <div className="segments-section">
                    <div className="section-title">🎬 Video Segments</div>
                    <div className="segments-grid">
                        {job.segments.map(seg => (
                            <div key={seg.segment_index} className={`segment-card ${seg.status === 'complete' ? 'seg-done' : ''}`}>
                                {seg.thumbnail_url ? (
                                    <img src={seg.thumbnail_url} alt={`Segment ${seg.segment_index + 1}`} className="seg-thumb" />
                                ) : (
                                    <div className="seg-thumb-placeholder">
                                        {seg.status === 'complete' ? '✓' : seg.status === 'queued' ? '⏳' : '🎬'}
                                    </div>
                                )}
                                <div className="seg-info">
                                    <div className="seg-num">Scene {seg.segment_index + 1}</div>
                                    {seg.room_type && <div className="seg-room">{seg.room_type.replace('_', ' ')}</div>}
                                    {seg.script_text && <div className="seg-script">"{seg.script_text.slice(0, 80)}..."</div>}
                                    <div className={`seg-status ${seg.status}`}>{seg.status || 'pending'}</div>
                                </div>
                            </div>
                        ))}
                    </div>
                </div>
            )}

            {/* Final video */}
            {isDone && job.final_video_url && (
                <div className="video-section">
                    <div className="section-title">🎉 Your Video is Ready</div>
                    <video
                        className="final-video"
                        src={job.final_video_url}
                        controls
                        autoPlay
                        playsInline
                    />
                    <div className="video-actions">
                        <a href={job.final_video_url} download className="download-btn">
                            ⬇️ Download MP4
                        </a>
                        <button className="new-btn" onClick={onReset}>
                            🎬 Generate Another
                        </button>
                    </div>
                </div>
            )}

            {/* Failed state */}
            {isFailed && (
                <div className="failed-section">
                    <p>❌ Video generation failed{job.error ? `: ${job.error}` : '.'}</p>
                    <button onClick={onReset}>Try Again</button>
                </div>
            )}

            {/* Processing indicator */}
            {!isDone && !isFailed && (
                <div className="processing-note">
                    <div className="spinner-sm" />
                    <span>Processing... this page updates automatically every 5 seconds</span>
                </div>
            )}
        </div>
    );
}
