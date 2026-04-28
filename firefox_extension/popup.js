document.getElementById('capture').addEventListener('click', async () => {
  const port = document.getElementById('port').value || 9876;
  const [tab] = await browser.tabs.query({active: true, currentWindow: true});
  const output = document.getElementById('output');
  
  output.textContent = `Captured to port ${port}:\nTitle: ${tab.title}\nURL: ${tab.url}\n\nState sent to bridge.`;
  
  try {
    await fetch(`http://localhost:${port}/update`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        url: tab.url,
        title: tab.title,
        timestamp: Date.now()
      })
    });
  } catch (e) {
    output.textContent += '\n\nBridge not running on that port.';
  }
});

document.getElementById('run').addEventListener('click', async () => {
  const code = document.getElementById('js').value.trim();
  const output = document.getElementById('output');
  if (!code) return;
  
  output.textContent = 'Executing...';
  
  const [tab] = await browser.tabs.query({active: true, currentWindow: true});
  
  try {
    // Execute code directly with output capture wrapper
    const wrappedCode = `
      (function() {
        const logs = [];
        const originalLog = console.log.bind(console);
        console.log = function(...args) {
          logs.push(args.join(' '));
          originalLog(...args);
        };
        
        let result;
        let error = null;
        try {
          result = ${code};
        } catch (e) {
          error = e.toString();
        }
        
        console.log = originalLog;
        return { result, logs, error };
      })()
    `;
    
    const results = await browser.tabs.executeScript(tab.id, { code: wrappedCode });
    const res = results[0];
    
    output.textContent = `Result: ${JSON.stringify(res.result, null, 2)}\n\nLogs:\n${res.logs.join('\n')}`;
    if (res.error) output.textContent += `\nError: ${res.error}`;
  } catch (e) {
    output.textContent = 'Error: ' + e.message;
  }
});
