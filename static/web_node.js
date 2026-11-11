// static/web_node.js
// FRACTUM Web Node Worker

// Calculate base path for loading assets
const basePath = self.location.href.substring(0, self.location.href.lastIndexOf('/'));

// Create a blob that injects the Smart Mount patch for pthreads
const ffmpegWorkerScript = `
self.Module = self.Module || {};
self.Module.preRun = self.Module.preRun || [];
self.Module.preRun.push(function() {
    // Smart Mount Patch: Allow FS.init() to run (create /dev etc) but prevent wiping root
    const FS = self.FS;
    const originalMount = FS.mount;
    FS.mount = function(type, opts, mountpoint) {
        if (mountpoint === '/' && type === FS.filesystems.MEMFS) {
            console.log("Ignored FS.mount('/', MEMFS) in worker to preserve file visibility.");
            return;
        }
        return originalMount(type, opts, mountpoint);
    };
});
importScripts('${basePath}/ffmpeg.js?v=${Date.now()}');
`;
const ffmpegWorkerBlob = new Blob([ffmpegWorkerScript], { type: 'application/javascript' });
const ffmpegWorkerUrl = URL.createObjectURL(ffmpegWorkerBlob);

self.Module = {
    print: function(text) { postMessage({type: 'log', level: 'sys', msg: "STDOUT: " + text}); },
    printErr: function(text) { postMessage({type: 'log', level: 'err', msg: "STDERR: " + text}); },
    onRuntimeInitialized: function() {
        // Only signal ready if FS is actually available
        if (self.Module.FS || self.FS) {
            postMessage({type: 'ready'});
        } else {
            postMessage({type: 'log', level: 'err', msg: "Runtime initialized but FS missing."});
        }
    },
    // CRITICAL: Point Pthreads to our proxy blob
    mainScriptUrlOrBlob: ffmpegWorkerUrl,
    // Since we are using a blob, we must tell Emscripten where to find the WASM file
    locateFile: function(path, scriptDirectory) {
        if (path.endsWith('.wasm')) {
            return basePath + "/ffmpeg.wasm";
        }
        return scriptDirectory + path;
    },
    noInitialRun: true,
    noExitRuntime: true,
    preRun: [function() {
        // Smart Mount Patch for Main Thread
        const FS = self.FS;
        const originalMount = FS.mount;
        FS.mount = function(type, opts, mountpoint) {
            if (mountpoint === '/' && type === FS.filesystems.MEMFS) {
                console.log("Ignored FS.mount('/', MEMFS) in main thread to preserve file visibility.");
                return;
            }
            return originalMount(type, opts, mountpoint);
        };
    }],
};

// Load FFmpeg WASM
importScripts('ffmpeg.js?v=' + Date.now());

// Global tracker for the expected output file
let currentOutputPath = null;

// Capture Emscripten's message handler (if any) to preserve Pthread communication
const emscriptenOnMessage = self.onmessage;

// We handle exit manually to detect job completion
self.Module.quit = function(status, toThrow) {
    postMessage({type: 'log', level: 'sys', msg: `FFmpeg exit with status ${status}`});
    if (self.resolveJob) {
        if (status === 0) {
            // Check if output exists to distinguish between "thread spawned" exit and "job done" exit
            const FS = self.Module.FS || self.FS;
            let exists = false;
            if (currentOutputPath) {
                try {
                    FS.stat(currentOutputPath);
                    exists = true;
                } catch(e) {}
            }

            if (exists) {
                self.resolveJob();
                self.resolveJob = null;
                self.rejectJob = null;
            } else {
                postMessage({type: 'log', level: 'sys', msg: "Ignored exit(0) - Output file not found (async spawn detection)."});
                // Do NOT clear callbacks, keep waiting for the real exit
            }
        } else {
            // Non-zero status is always an error/end
            self.rejectJob(new Error(`FFmpeg exited with status ${status}`));
            self.resolveJob = null;
            self.rejectJob = null;
        }
    }
    // Emscripten expects quit to throw to stop execution
    if (toThrow) throw toThrow;
};

self.onmessage = async function(e) {
    const msg = e.data;
    
    // Handle our custom job messages
    if (msg && msg.type === 'run_job') {
        try {
            await processJob(msg.job);
        } catch (err) {
            postMessage({type: 'error', msg: err.toString(), restart: runtimeStuck});
        }
        return; // Don't pass job messages to Emscripten
    }

    // Chunk of a large video: encode one time-slice from a pre-cut segment.
    if (msg && msg.type === 'run_chunk') {
        try {
            await processChunk(msg.chunk);
        } catch (err) {
            postMessage({type: 'chunk_error', msg: err.toString(), restart: runtimeStuck});
        }
        return;
    }

    // Pass everything else (e.g. Pthread messages) to Emscripten
    if (emscriptenOnMessage) {
        emscriptenOnMessage(e);
    }
};

// Encode one video chunk of a large source. The manager streams a small,
// keyframe-aligned segment (with an accurate lead offset in the response
// headers); we seek into it and encode exactly [start, start+dur], matching
// what a native chunk worker produces so assembly tiles perfectly.
// FFmpeg runs on its own thread (PROXY_TO_PTHREAD), so this worker stays free
// while it encodes. FFmpeg appends key=value progress blocks to a file in the
// in-memory filesystem; read the latest encoded position from it and pass it
// to the page, which shows it and sends it to the manager as real progress.
// (-progress and -stats_period only control reporting, not the encode.)
//
// It is also how completion is detected. This build writes the whole output
// and prints its final report, then hangs in its own shutdown and never calls
// exit. FFmpeg writes "progress=end" with that final report, after the output
// file has been finalised and closed, so that marker is taken as "done". The
// runtime is then left stuck in shutdown: `runtimeStuck` tells the page to
// replace this worker before the next job.
let runtimeStuck = false;

function finishWithoutExit() {
    if (!self.resolveJob) return;      // exit arrived normally after all
    runtimeStuck = true;
    postMessage({type: 'log', level: 'sys', msg: "FFmpeg finished (no exit signal); continuing."});
    const done = self.resolveJob;
    self.resolveJob = null;
    self.rejectJob = null;
    done();
}

function startProgressPoll(FS, progPath, totalSec) {
    let lastSec = -1;
    let ended = false;
    const id = setInterval(() => {
        try {
            const txt = FS.readFile(progPath, { encoding: 'utf8' });
            if (!ended && txt.indexOf('progress=end') >= 0) {
                ended = true;
                // A moment for a normal exit to arrive first, if it is going to.
                setTimeout(finishWithoutExit, 3000);
            }
            const at = txt.lastIndexOf('out_time_us=');
            if (at < 0) return;
            const us = parseInt(txt.slice(at + 12, at + 40), 10);
            if (!Number.isFinite(us) || us < 0) return;
            const sec = us / 1e6;
            if (sec <= lastSec) return;
            lastSec = sec;
            const pct = (totalSec > 0) ? Math.max(0, Math.min(99, Math.floor(sec / totalSec * 100))) : null;
            postMessage({ type: 'progress', sec: sec, pct: pct });
        } catch (e) { /* file not created yet */ }
    }, 5000);
    return () => clearInterval(id);
}

async function processChunk(chunk) {
    const FS = self.Module.FS || self.FS;
    const callMain = self.Module.callMain || self.callMain;
    if (!FS || !callMain) throw new Error(`FFmpeg primitives missing. FS:${!!FS} callMain:${!!callMain}`);

    const segPath = "/tmp/seg_" + Date.now() + ".mkv";
    const outPath = "/tmp/cout_" + Date.now() + ".mp4";
    const progPath = "/tmp/cprog_" + Date.now() + ".txt";
    let stopProgress = () => {};
    const crf = (chunk.content_profile === 'live_action') ? '57' : '63';

    postMessage({type: 'log', level: 'sys', msg: `Chunk ${chunk.chunk_index} of ${chunk.filename}: downloading segment...`});
    try {
        const headers = chunk.token ? { 'X-Worker-Token': chunk.token } : {};
        const resp = await fetch(chunk.segment_url, { headers });
        if (!resp.ok) throw new Error("Segment download failed: " + resp.status);
        // Accurate inner seek offset + duration come from the manager.
        const lead = parseFloat(resp.headers.get('X-Segment-Lead') || '0') || 0;
        const dur  = parseFloat(resp.headers.get('X-Segment-Duration') || String(chunk.duration_sec)) || chunk.duration_sec;

        const data = new Uint8Array(await resp.arrayBuffer());
        try { FS.mkdir('/tmp'); } catch(e) {}
        FS.writeFile(segPath, data);
        postMessage({type: 'log', level: 'sys', msg: `Segment ${data.length} bytes; encoding [lead=${lead.toFixed(2)}s dur=${dur.toFixed(2)}s]...`});

        // Same targets as the native chunk path: video-only, SVT-AV1 preset 2,
        // CRF (profile-aware), 480p, timestamps re-zeroed for clean concat.
        // lp=1 only limits encoder threading (browser memory), it does not
        // change the bitstream — no tune override, so browser chunks match
        // native chunks (SVT default tune) when tiled into one video.
        // -enc_time_base pins the encoder time base to the source frame rate
        // (manager sends the exact fraction): FFmpeg 7.x (this wasm) does not
        // propagate a frame rate through setpts, and libsvtav1 would see
        // 1/time_base = 1000 fps and refuse to start (240 fps cap).
        let etbArgs = [];
        const frMatch = String(chunk.fps || '').match(/^([1-9]\d*)\/([1-9]\d*)$/);
        if (frMatch) {
            const fn = parseInt(frMatch[1], 10), fd = parseInt(frMatch[2], 10);
            // setts: this FFmpeg build hands the muxer packets with no
            // duration, so the MP4's length came out one frame short and
            // every chunk after a browser chunk landed a frame early when
            // the film was assembled. With the encoder time base pinned to
            // one frame, each packet lasts exactly 1 tick. Timestamps only;
            // the encoded video is untouched.
            if (fn / fd <= 240) etbArgs = ['-enc_time_base', `${fd}/${fn}`, '-bsf:v', 'setts=duration=1'];
        }
        const args = [
            '-threads', '1', '-v', 'verbose',
            '-ss', lead.toFixed(3), '-i', segPath, '-t', dur.toFixed(3),
            '-map', '0:v:0', '-an', '-sn',
            '-c:v', 'libsvtav1', '-preset', '2', '-crf', crf,
            '-pix_fmt', 'yuv420p',
            '-svtav1-params', 'lp=1',
            ...etbArgs,
            '-vf', 'setpts=PTS-STARTPTS,scale=-2:480',
            '-movflags', '+faststart',
            '-progress', progPath, '-stats_period', '5',
            outPath
        ];
        currentOutputPath = outPath;
        const ffmpegPromise = new Promise((resolve, reject) => { self.resolveJob = resolve; self.rejectJob = reject; });
        callMain(args);
        stopProgress = startProgressPoll(FS, progPath, dur);
        await ffmpegPromise;
        stopProgress();

        let exists = false;
        try { FS.stat(outPath); exists = true; } catch(e) {}
        if (!exists) throw new Error("FFmpeg produced no chunk output.");
        const outData = FS.readFile(outPath);
        const blob = new Blob([outData], { type: 'video/mp4' });

        postMessage({type: 'log', level: 'sys', msg: `Uploading chunk (${outData.length} bytes)...`});
        const fd = new FormData();
        fd.append('job_id', chunk.job_id);
        fd.append('worker_id', chunk.worker_id);
        fd.append('kind', 'video');
        fd.append('chunk_index', chunk.chunk_index);
        if (chunk.wallet) fd.append('wallet', chunk.wallet);
        fd.append('file', blob, `chunk_${chunk.chunk_index}.mp4`);
        const up = await fetch('/upload_chunk', {
            method: 'POST', body: fd,
            headers: chunk.token ? { 'X-Worker-Token': chunk.token } : {}
        });
        if (up.status === 409) { postMessage({type: 'chunk_stale', restart: runtimeStuck}); return; }
        if (!up.ok) throw new Error("Chunk upload failed: " + up.status);
        postMessage({type: 'chunk_done', restart: runtimeStuck});
    } finally {
        stopProgress();
        currentOutputPath = null;
        try { FS.unlink(segPath); } catch(e) {}
        try { FS.unlink(outPath); } catch(e) {}
        try { FS.unlink(progPath); } catch(e) {}
    }
}

async function processJob(job) {
    // FS and callMain might be on Module or global scope depending on Emscripten build options
    const FS = self.Module.FS || self.FS;
    const callMain = self.Module.callMain || self.callMain;
    
    if (!FS || !callMain) {
        throw new Error(`FFmpeg primitives missing. FS: ${!!FS}, callMain: ${!!callMain}`);
    }

    const inputFilename = "input_" + Date.now() + ".mp4";
    const outputFilename = "output_" + Date.now() + ".mp4";
    const inputPath = "/tmp/" + inputFilename;
    const outputPath = "/tmp/" + outputFilename;
    const progPath = "/tmp/prog_" + Date.now() + ".txt";
    let stopProgress = () => {};

    postMessage({type: 'log', level: 'sys', msg: `Worker processing: ${job.filename}`});

    try {
        // 1. Download — ALWAYS via the manager (same-origin). The /web page is
        // cross-origin isolated (COEP: require-corp), so fetching a remote
        // source directly (job.download_url can point off-origin) fails with a
        // NetworkError. /download_media proxies remote sources and serves local
        // ones, so the browser always sees a same-origin response.
        postMessage({type: 'log', level: 'sys', msg: "Downloading source (via manager)..."});
        const mediaUrl = '/download_media?job_id=' + encodeURIComponent(job.id);
        const resp = await fetch(mediaUrl, { headers: job.token ? { 'X-Worker-Token': job.token } : {} });
        if (!resp.ok) {
            // Worded so the manager recognises a source outage (no strike
            // against the job) rather than a failed encode.
            if (resp.status >= 500 || resp.status === 429) {
                throw new Error("Could not read the source (HTTP error " + resp.status + ")");
            }
            throw new Error("Download failed: " + resp.status);
        }
        
        const buf = await resp.arrayBuffer();
        const data = new Uint8Array(buf);
        
        // 2. Write to MEMFS
        postMessage({type: 'log', level: 'sys', msg: `Writing ${data.length} bytes to ${inputPath}`});
        
        // Ensure /tmp exists
        try { FS.mkdir('/tmp'); } catch(e) {}
        
        FS.writeFile(inputPath, data);
        
        // Verify input exists (sanity check)
        try {
            const stat = FS.stat(inputPath);
            postMessage({type: 'log', level: 'sys', msg: `Input file verified on FS: ${stat.size} bytes`});
        } catch(e) {
            throw new Error(`Failed to verify input file at ${inputPath} after write.`);
        }
        
        // 3. Execute
        postMessage({type: 'log', level: 'sys', msg: "Starting FFmpeg..."});
        
        // STRICT ENCODING CONFIGURATION
        // Matches the manager's verified target: SVT-AV1 preset 2 / CRF 63
        // (57 for the live_action profile, same as native workers), 480p,
        // mono Opus 24k. Notes specific to the browser build:
        //  - This wasm has --disable-ffprobe, so we can't inspect streams. We map
        //    the first video + first (optional) audio explicitly and drop
        //    subtitles (-sn): blindly transcoding an unknown subtitle codec to
        //    mov_text is a common hard-failure, and bitmap subs can't convert.
        //  - lp=1 keeps browser memory down (native workers pass no
        //    -svtav1-params; lp only limits threading, not the bitstream).
        //  - Audio is encoded like the native workers do it: libopus, 24k,
        //    mono. When the manager sent the source's stream layout, the same
        //    track is picked and the same mono downmix is applied (centre
        //    channel favoured on surround sources so dialogue stays clear).
        //    Without that layout, the first audio track is taken if there is
        //    one and FFmpeg's default downmix is used.
        let audioMap = ['-map', '0:a:0?'];
        let audioFilter = [];
        const sm = job.stream_meta || null;
        if (sm && sm.audio_index !== null && sm.audio_index !== undefined) {
            const idx = parseInt(sm.audio_index, 10);
            const ch = parseInt(sm.audio_channels, 10);
            if (Number.isFinite(idx) && idx >= 0 && Number.isFinite(ch) && ch >= 1) {
                audioMap = ['-map', `0:${idx}`];
                const af = (ch > 2) ? 'pan=mono|c0=0.5*FC+0.25*FL+0.25*FR+0.1*BL+0.1*BR,aresample=async=1'
                         : (ch === 2) ? 'pan=mono|c0=0.5*c0+0.5*c1,aresample=async=1'
                         : 'aresample=async=1';
                audioFilter = ['-af', af];
            }
        }
        const crf = (job.content_profile === 'live_action') ? '57' : '63';
        const args = [
            '-threads', '1',
            '-v', 'verbose',
            '-i', inputPath,
            '-map', '0:v:0',
            ...audioMap,
            '-sn',
            '-c:v', 'libsvtav1',
            '-preset', '2',
            '-crf', crf,
            '-pix_fmt', 'yuv420p',
            '-svtav1-params', 'lp=1',
            '-vf', 'scale=-2:480',
            '-c:a', 'libopus',
            '-b:a', '24k',
            '-ac', '1',
            ...audioFilter,
            '-progress', progPath, '-stats_period', '5',
            outputPath
        ];

        // Set expected output path for quit handler
        currentOutputPath = outputPath;

        // Wrap execution in a promise to wait for completion
        const ffmpegPromise = new Promise((resolve, reject) => {
            self.resolveJob = resolve;
            self.rejectJob = reject;
        });

        // callMain might return immediately if proxied, or block. 
        // We await our custom promise which is triggered by Module.quit
        callMain(args);
        const totalSec = parseFloat((job.stream_meta && job.stream_meta.duration) || 0) || 0;
        stopProgress = startProgressPoll(FS, progPath, totalSec);

        await ffmpegPromise;
        stopProgress();

        // 4. Read Output
        postMessage({type: 'log', level: 'sys', msg: "Reading output..."});
        
        // Verify output exists
        let exists = false;
        try {
            const stat = FS.stat(outputPath);
            exists = true;
            postMessage({type: 'log', level: 'sys', msg: `Output file size: ${stat.size} bytes`});
        } catch(e) { exists = false; }

        if (!exists) {
            // Debug: List /tmp to see what happened
            try {
                postMessage({type: 'log', level: 'err', msg: `/tmp content: ${JSON.stringify(FS.readdir('/tmp'))}`});
            } catch(e){}
            throw new Error("FFmpeg did not create output file (check logs for errors).");
        }
        
        const outData = FS.readFile(outputPath);
        const blob = new Blob([outData], { type: 'video/mp4' });
        
        // 5. Upload
        postMessage({type: 'log', level: 'sys', msg: `Uploading ${outData.length} bytes...`});
        
        const formData = new FormData();
        formData.append('job_id', job.id);
        formData.append('worker_id', job.worker_id);
        if (job.wallet) formData.append('wallet', job.wallet);
        formData.append('file', blob, 'result.mp4');

        // Auth: the manager now requires the worker token (REQUIRE_WORKER_TOKEN).
        // Sent as a header so it isn't logged in the URL.
        const upHeaders = job.token ? { 'X-Worker-Token': job.token } : {};
        const up = await fetch('/upload_result', { method: 'POST', body: formData, headers: upHeaders });
        if (up.status === 400 || up.status === 409) {
            let why = "HTTP " + up.status;
            try { const j = await up.json(); if (j && j.message) why += " " + j.message; } catch (e) {}
            postMessage({type: 'upload_rejected', msg: why, restart: runtimeStuck});
            return;
        }
        if (!up.ok) throw new Error("Upload failed: " + up.status + " " + up.statusText);
        
        postMessage({type: 'done', restart: runtimeStuck});

    } catch (e) {
        throw e;
    } finally {
        stopProgress();
        currentOutputPath = null;
        // Cleanup
        try {
            if (FS) {
                try { FS.unlink(progPath); } catch(e) {}
                try { FS.unlink(inputPath); } catch(e) {}
                try { FS.unlink(outputPath); } catch(e) {}
            }
        } catch (e) {
            postMessage({type: 'log', level: 'err', msg: "Cleanup error: " + e.message});
        }
    }
}