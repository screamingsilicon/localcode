// LocalCode Browser Bridge - Background Script (Firefox)

let lastState = {
  url: '',
  title: '',
  timestamp: 0,
  logs: []
};

let pendingCommand = null;

// Poll bridge for commands
async function pollBridge() {
  try {
    const res = await fetch('http://localhost:9876/command', {
      method: 'GET',
      headers: { 'Accept': 'application/json' }
    });
    console.log('LocalCode: pollBridge response status:', res.status);
    if (res.ok) {
      const data = await res.json();
      console.log('LocalCode: pollBridge data:', data);
      if (data.command === 'execute' && data.code) {
        pendingCommand = data;
        console.log('LocalCode: executing code');
        await executeAndSendResult(data.code);
      }
    }
  } catch (e) {
    console.error('LocalCode: pollBridge error:', e);
  }
}

async function executeAndSendResult(code) {
  try {
    const [tab] = await browser.tabs.query({active: true, currentWindow: true});
    if (!tab) {
      await sendResultToBridge({ error: 'No active tab found' });
      return;
    }

    // Check if code is a navigation command
    const navMatch = code.match(/(?:window|document)?\.location(?:\.href)?\s*=\s*['"]([^'"]+)['"]/);
    if (navMatch) {
      const url = navMatch[1];
      console.log('LocalCode: Navigating to:', url);
      await browser.tabs.update(tab.id, { url: url });
      await sendResultToBridge({ result: `Navigated to ${url}`, logs: [`Navigated to ${url}`], error: null });
      return;
    }

    // Execute code directly with output capture wrapper
    let executionResult;
    await new Promise((resolve, reject) => {
      // Wrap user code to capture console.log and errors
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
      
      browser.tabs.executeScript(tab.id, { code: wrappedCode }, (result) => {
        console.log('LocalCode: executeScript result:', result);
        console.log('LocalCode: executeScript result[0]:', result && result[0]);
        if (browser.runtime.lastError) {
          console.error('LocalCode: executeScript error:', browser.runtime.lastError);
          reject(browser.runtime.lastError);
        } else {
          executionResult = result[0];
          console.log('LocalCode: executionResult set to:', executionResult);
          resolve();
        }
      });
    });
    await sendResultToBridge(executionResult);
  } catch (e) {
    await sendResultToBridge({ error: e.toString() });
  }
}

async function sendResultToBridge(result) {
  try {
    await fetch('http://localhost:9876/result', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(result)
    });
    console.log('LocalCode: Result sent to bridge');
  } catch (e) {
    console.error('LocalCode: Failed to send result to bridge:', e);
  }
}

// Update state periodically and send to bridge
async function updateBridgeState() {
  try {
    const [tab] = await browser.tabs.query({active: true, currentWindow: true});
    if (tab) {
      lastState.url = tab.url || '';
      lastState.title = tab.title || '';
      lastState.timestamp = Date.now();
      
      // Send state to bridge
      await fetch('http://localhost:9876/update', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          url: lastState.url,
          title: lastState.title,
          timestamp: lastState.timestamp
        })
      });
      console.log('LocalCode: State updated to bridge');
    }
  } catch (e) {
    // Silently fail if bridge is not running
  }
}

setInterval(updateBridgeState, 2000);
setInterval(pollBridge, 1500);

// Keep background script alive
setInterval(() => {
  // No-op to prevent Firefox from suspending the background script
}, 5000);

console.log('LocalCode Bridge background loaded (Firefox)');
