# Desktop seller-switch test status

## Verified

- Published and installed revision: `b06f99e60e1e8978aea25601775a06ea2a508dfb`.
- Python suite: 45 tests passed. This includes one runner executing 17 real React interaction scenarios with a simulated SDK.
- Hermes plugin validation passed with zero warnings; runtime doctor passed.
- Standalone FastAPI backend load and GET seller-options tested through TestClient. Fixed the former `import proxy_reader` failure.
- Added read-only route validation endpoint. Invalid exact seller pins reject before inference.
- Proxy health on port 8378 returns HTTP 200. No paid inference request was sent in this verification.

## Not verified

The installed desktop has not loaded the repaired card and no live mid-chat switch or post-switch completion has been demonstrated. The installed Python plugin exists and is enabled, but the desktop copy at `$HERMES_HOME/desktop-plugins/llmux/plugin.js` was absent at the final check. Plugin validation and the React shim do not establish actual desktop execution.

The GUI driver listed the running Hermes window but intermittently returned empty accessibility trees. A command-palette typing action landed `Reload desktop plugins` in the composer of the visible chat instead. No Enter/send action followed. Input automation was stopped to avoid interfering with the owner's active work. The draft may need that text removed by the owner.

## Resume gate

The owner should use Cmd+K > Reload desktop plugins, then enable LLMUX's desktop half in the plugin settings. Continue with a separate test chat, not an existing project conversation. Confirm the exact model and seller, display context-derived uncached input estimate, validate the route, verify the gateway's applied/deferred response, and observe the next response and selected upstream. Never call the simulated-host tests live proof.

## Remaining implementation limitations

- The card stops the active turn and selects a route for the next turn. It does not migrate or automatically regenerate an interrupted response.
- Surplus's current comparison row represents the marketplace, not a verified exact seller pin.
- Speed fields are upstream medians; predicted completion time assumes 1000 output tokens and is not an observed mean response time.
- The read-only proxy started for this test remains on 127.0.0.1:8378, without a Surplus credential supplied by this test.
