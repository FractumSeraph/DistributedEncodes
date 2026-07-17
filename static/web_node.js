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
            postMessage({type: 'error', msg: err.toString()});
        }
        return; // Don't pass job messages to Emscripten
    }

    // Pass everything else (e.g. Pthread messages) to Emscripten
    if (emscriptenOnMessage) {
        emscriptenOnMessage(e);
    }
};

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

    postMessage({type: 'log', level: 'sys', msg: `Worker processing: ${job.filename}`});

    try {
        // 1. Download
        postMessage({type: 'log', level: 'sys', msg: "Downloading source..."});
        const resp = await fetch(job.download_url);
        if (!resp.ok) throw new Error("Download failed: " + resp.status);
        
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
        const args = [
            '-threads', '1', 
            '-v', 'verbose',
            '-i', inputPath,
            '-c:v', 'libsvtav1',
            '-preset', '2',
            '-crf', '63',
            '-g', '240',
            '-pix_fmt', 'yuv420p', 
            '-svtav1-params', 'tune=0:lp=1',
            '-vf', 'scale=-2:480',
            '-c:a', 'opus',
            '-b:a', '12k',
            '-ac', '1',
            '-strict', '-2',
            '-c:s', 'mov_text',
            '-svtav1-params', 'logical-processors=2',
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
        
        await ffmpegPromise;

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
        formData.append('file', blob, 'result.mp4');

        const up = await fetch('/upload_result', { method: 'POST', body: formData });
        if (!up.ok) throw new Error("Upload failed: " + up.statusText);
        
        postMessage({type: 'done'});

    } catch (e) {
        throw e;
    } finally {
        currentOutputPath = null;
        // Cleanup
        try {
            if (FS) {
                try { FS.unlink(inputPath); } catch(e) {}
                try { FS.unlink(outputPath); } catch(e) {}
            }
        } catch (e) {
            postMessage({type: 'log', level: 'err', msg: "Cleanup error: " + e.message});
        }
    }
}