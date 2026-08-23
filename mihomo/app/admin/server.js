const http = require('http');
const fs = require('fs');
const path = require('path');
const { execSync, spawn } = require('child_process');

const PORT = 9091;
const CONFIG_PATH = '/vol1/@appdata/fnnas.fnsoar/config.yaml';
const PID_PATH = '/vol1/@appdata/fnnas.fnsoar/clashmini.pid';
const MIHOMO_BIN = '/vol1/@appcenter/fnnas.fnsoar/bin/mihomo-amd64';

// Simple YAML parser for proxy-providers
function parseProxyProviders(yaml) {
    // Find proxy-providers section
    const lines = yaml.split('\n');
    const startIdx = lines.findIndex(l => l.trim().startsWith('proxy-providers:'));
    if (startIdx === -1) return {};
    
    const providers = {};
    let current = null;
    let indent = null;
    
    for (let i = startIdx + 1; i < lines.length; i++) {
        const line = lines[i];
        if (line.trim() === '' || line.trim().startsWith('#')) continue;
        if (line.startsWith('  ') === false) break; // End of proxy-providers section
        
        const match = line.match(/^(\s+)([\w\u4e00-\u9fff-]+):\s*(.*)$/);
        if (match) {
            const name = match[2];
            const value = match[3];
            if (value === '') {
                current = name;
                providers[current] = {};
            } else {
                providers[current] = Object.assign(providers[current] || {}, { raw: value });
            }
        } else if (current && line.match(/^\s+type:\s*(.*)$/)) {
            providers[current].type = line.match(/type:\s*(.*)$/)[1];
        } else if (current && line.match(/^\s+url:\s*(.*)$/)) {
            providers[current].url = line.match(/url:\s*(.*)$/)[1].replace(/"/g, '');
        } else if (current && line.match(/^\s+interval:\s*(.*)$/)) {
            providers[current].interval = line.match(/interval:\s*(.*)$/)[1];
        }
    }
    return providers;
}

function writeConfig(yaml) {
    fs.writeFileSync(CONFIG_PATH, yaml, 'utf8');
}

function getConfig() {
    try {
        return fs.readFileSync(CONFIG_PATH, 'utf8');
    } catch (e) {
        return '';
    }
}

function startService() {
    try {
        execSync('appcenter-cli start fnnas.fnsoar', {stdio: 'ignore'});
        return {success: true};
    } catch (e) {
        return {success: false, error: e.message};
    }
}

function stopService() {
    try {
        execSync('appcenter-cli stop fnnas.fnsoar', {stdio: 'ignore'});
        return {success: true};
    } catch (e) {
        return {success: false, error: e.message};
    }
}

function getStatus() {
    const pid = fs.readFileSync(PID_PATH, 'utf8').trim();
    const running = pid && fs.existsSync(`/proc/${pid}`);
    return {running, pid};
}

// Serve static files
const MIME = {
    '.html': 'text/html',
    '.css': 'text/css',
    '.js': 'application/javascript',
    '.json': 'application/json',
    '.png': 'image/png',
    '.svg': 'image/svg+xml',
};

const server = http.createServer((req, res) => {
    const url = new URL(req.url, `http://${req.headers.host}`);
    const pathname = url.pathname;
    
    // API routes
    if (pathname.startsWith('/api/')) {
        res.setHeader('Content-Type', 'application/json');
        
        if (pathname === '/api/status') {
            res.end(JSON.stringify(getStatus()));
        } else if (pathname === '/api/config' && req.method === 'GET') {
            res.end(JSON.stringify({config: getConfig()}));
        } else if (pathname === '/api/config' && req.method === 'POST') {
            let body = '';
            req.on('data', chunk => body += chunk);
            req.on('end', () => {
                try {
                    const data = JSON.parse(body);
                    writeConfig(data.config);
                    // Restart mihomo to apply
                    execSync('appcenter-cli restart fnnas.fnsoar', {stdio: 'ignore'});
                    res.end(JSON.stringify({success: true}));
                } catch (e) {
                    res.statusCode = 500;
                    res.end(JSON.stringify({success: false, error: e.message}));
                }
            });
        } else if (pathname === '/api/service/start' && req.method === 'POST') {
            res.end(JSON.stringify(startService()));
        } else if (pathname === '/api/service/stop' && req.method === 'POST') {
            res.end(JSON.stringify(stopService()));
        } else if (pathname === '/api/proxy-providers' && req.method === 'GET') {
            res.end(JSON.stringify({providers: parseProxyProviders(getConfig())}));
        } else {
            res.statusCode = 404;
            res.end(JSON.stringify({error: 'Not found'}));
        }
        return;
    }
    
    // Serve index.html for root
    if (pathname === '/') {
        res.setHeader('Content-Type', 'text/html');
        res.end(fs.readFileSync(path.join(__dirname, 'index.html'), 'utf8'));
        return;
    }
    
    // 404
    res.statusCode = 404;
    res.end('Not found');
});

server.listen(PORT, '0.0.0.0', () => {
    console.log(`FnSoar Admin Server running on port ${PORT}`);
});

module.exports = {parseProxyProviders, writeConfig, getConfig, startService, stopService, getStatus};
