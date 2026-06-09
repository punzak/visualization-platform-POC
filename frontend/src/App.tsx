import React, { useState } from 'react';
import UploadPage from './components/UploadPage';
import JobStatus from './components/JobStatus';
import './App.css';

export type AppState = 'upload' | 'processing' | 'done';

export default function App() {
    const [state, setState] = useState<AppState>('upload');
    const [jobId, setJobId] = useState<string | null>(null);

    const handleJobStarted = (id: string) => {
        setJobId(id);
        setState('processing');
    };

    const handleReset = () => {
        setJobId(null);
        setState('upload');
    };

    return (
        <div className="app">
            <header className="header">
                <div className="header-inner">
                    <div className="logo">
                        <span className="logo-icon">🎬</span>
                        <div>
                            <h1>Property Video Generator</h1>
                            <p>Upload photos · Generate story · Create video</p>
                        </div>
                    </div>
                    {state !== 'upload' && (
                        <button className="reset-btn" onClick={handleReset}>← New Video</button>
                    )}
                </div>
            </header>
            <main className="main">
                {state === 'upload' && <UploadPage onJobStarted={handleJobStarted} />}
                {(state === 'processing' || state === 'done') && jobId && (
                    <JobStatus jobId={jobId} onReset={handleReset} />
                )}
            </main>
        </div>
    );
}
