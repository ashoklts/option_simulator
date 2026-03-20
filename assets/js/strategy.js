/**
 * strategy.js — Option Chain Strategy Execution
 *
 * Manages all strategy logic for the position simulator.
 * Currently implements: Mini Strangle
 *
 * Reads globals from index.html:
 *   option_chain_data, optionLegs, calcCurrentPnL,
 *   renderLegs, renderPositionTable, updateSummaryStats, updateChart,
 *   window.reloadOptionChain, window._ocGetTimestamp
 */

(function (global) {
    'use strict';

    // ─── helpers ───────────────────────────────────────────────────────────

    function pad(n) { return String(n).padStart(2, '0'); }

    /** Returns the month/year of the currently-selected option-chain timestamp. */
    function getCurrentYearMonth() {
        var ts = typeof global._ocGetTimestamp === 'function'
            ? global._ocGetTimestamp()
            : new Date().toISOString();
        var p = ts.match(/^(\d{4})-(\d{2})/);
        return p ? { year: +p[1], month: +p[2] - 1 } : null; // month is 0-indexed
    }

    // ─── Step 1 · expiry tab selection ────────────────────────────────────

    /**
     * Finds and clicks the correct expiry tab, then returns its date string
     * (e.g. "2025-10-07") so downstream logic can filter option_chain_data.
     *
     * @param {'CURRENT_WEEK'|'NEXT_WEEK'|'MONTHLY_EXPIRY'} expiryType
     * @returns {string|null} expiry date string or null on failure
     */
    function selectExpiryTab(expiryType) {
        // After renderCarousel(), buttons have data-oc-expiry attributes.
        var buttons = Array.from(
            document.querySelectorAll('.expiry_button[data-oc-expiry]')
        );

        if (!buttons.length) {
            console.warn('[Strategy] No .expiry_button[data-oc-expiry] elements found.');
            return null;
        }

        var target = null;

        if (expiryType === 'CURRENT_WEEK') {
            target = buttons[0] || null;

        } else if (expiryType === 'NEXT_WEEK') {
            target = buttons[1] || null;

        } else if (expiryType === 'MONTHLY_EXPIRY') {
            var ym = getCurrentYearMonth();
            if (ym) {
                // collect all expiry buttons whose date falls in the current month
                var inMonth = buttons.filter(function (btn) {
                    var d = new Date(btn.dataset.ocExpiry);
                    return d.getFullYear() === ym.year && d.getMonth() === ym.month;
                });
                // pick the last one (latest expiry of the month)
                target = inMonth.length ? inMonth[inMonth.length - 1] : null;
            }
        }

        if (!target) {
            console.warn('[Strategy] No expiry tab found for type:', expiryType);
            return null;
        }

        // Clicking the button activates the tab and calls renderTable() synchronously.
        target.click();
        return target.dataset.ocExpiry; // "YYYY-MM-DD"
    }

    // ─── Step 2 · strike selection (Mini Strangle) ────────────────────────

    /**
     * Maps the 5th OTM CE premium to the OTM strike index to sell.
     *
     * Premium range  → OTM index to sell
     * < 100          → 4th (or 5th if current PnL > 0)
     * 100 – 160      → 5th
     * 160 – 270      → 6th
     * 270 – 370      → 7th
     * 370 – 470      → 8th
     * > 470          → 9th
     */
    function resolveTargetOtmIndex(fifthOtmPremium) {
        if (fifthOtmPremium < 100) {
            var pnl = typeof calcCurrentPnL === 'function' ? calcCurrentPnL() : 0;
            return pnl > 0 ? 5 : 4;
        }
        if (fifthOtmPremium <= 160) return 5;
        if (fifthOtmPremium <= 270) return 6;
        if (fifthOtmPremium <= 370) return 7;
        if (fifthOtmPremium <= 470) return 8;
        return 9;
    }

    // ─── Step 3 · position creation ───────────────────────────────────────

    function alreadySold(expiry, strike, optionType) {
        if (typeof optionLegs === 'undefined') return false;
        return optionLegs.some(function (leg) {
            return leg.expiry     === expiry  &&
                   leg.strike     === strike  &&
                   leg.optionType === optionType &&
                   leg.type       === 'Sell';
        });
    }

    function refreshUI() {
        if (typeof renderLegs          === 'function') renderLegs();
        if (typeof renderPositionTable === 'function') renderPositionTable();
        if (typeof updateSummaryStats  === 'function') updateSummaryStats();
        if (typeof updateChart         === 'function') updateChart();
        // re-render option chain to reflect active Sell button states
        if (typeof global.reloadOptionChain === 'function') global.reloadOptionChain();
    }

    // ─── Mini Strangle ────────────────────────────────────────────────────

    function executeMiniStrangle(expiryType) {

        // ── 1. activate expiry tab ───────────────────────────────────────
        var expiry = selectExpiryTab(expiryType);
        if (!expiry) return;

        // ── 2. get option chain rows for this expiry ─────────────────────
        if (typeof option_chain_data === 'undefined' || !option_chain_data.length) {
            console.warn('[Strategy] option_chain_data is empty.');
            return;
        }

        var rows = option_chain_data.filter(function (r) { return r.expiry === expiry; });
        if (!rows.length) {
            console.warn('[Strategy] No option chain rows for expiry:', expiry);
            return;
        }

        // ── 3. build strike map (ascending) ─────────────────────────────
        var map = {};
        rows.forEach(function (r) {
            if (!map[r.strike]) map[r.strike] = { call: null, put: null };
            if      (r.type === 'CE') map[r.strike].call = r;
            else if (r.type === 'PE') map[r.strike].put  = r;
        });
        var strikes = Object.keys(map).map(Number).sort(function (a, b) { return a - b; });

        // ── 4. ATM strike ────────────────────────────────────────────────
        var spotPrice = rows[0].spot_price;
        var atmStrike = strikes.reduce(function (best, cur) {
            return Math.abs(cur - spotPrice) < Math.abs(best - spotPrice) ? cur : best;
        }, strikes[0]);
        var atmIdx = strikes.indexOf(atmStrike);

        // ── 5. 5th OTM CE premium ────────────────────────────────────────
        var idx5th = atmIdx + 5;
        if (idx5th >= strikes.length) {
            console.warn('[Strategy] Not enough OTM CE strikes (need at least 5 above ATM).');
            return;
        }
        var row5th       = map[strikes[idx5th]];
        var fifthPremium = (row5th && row5th.call) ? row5th.call.close : 0;

        // ── 6. resolve which OTM index to sell ──────────────────────────
        var targetN = resolveTargetOtmIndex(fifthPremium);
        var ceIdx   = atmIdx + targetN;
        var peIdx   = atmIdx - targetN;

        if (ceIdx >= strikes.length) {
            console.warn('[Strategy] CE target strike out of range (index ' + ceIdx + ').');
            return;
        }
        if (peIdx < 0) {
            console.warn('[Strategy] PE target strike out of range (index ' + peIdx + ').');
            return;
        }

        var ceStrike  = strikes[ceIdx];
        var peStrike  = strikes[peIdx];
        var cePremium = (map[ceStrike].call) ? map[ceStrike].call.close : 0;
        var pePremium = (map[peStrike].put)  ? map[peStrike].put.close  : 0;

        console.info(
            '[Strategy] Mini Strangle → expiry:', expiry,
            '| ATM:', atmStrike,
            '| 5th OTM premium: ₹' + fifthPremium.toFixed(2),
            '| target OTM idx:', targetN,
            '| SELL CE:', ceStrike, '@ ₹' + cePremium,
            '| SELL PE:', peStrike, '@ ₹' + pePremium
        );

        // ── 7. push SELL legs (skip if already open) ────────────────────
        if (typeof optionLegs === 'undefined') {
            console.warn('[Strategy] optionLegs global not found.');
            return;
        }

        var chainTs = option_chain_data[0].timestamp;
        var added   = false;

        if (!alreadySold(expiry, ceStrike, 'Call')) {
            optionLegs.push({
                type:       'Sell',
                optionType: 'Call',
                strike:     ceStrike,
                premium:    cePremium,
                quantity:   65,
                expiry:     expiry,
                entryDate:  chainTs
            });
            added = true;
        } else {
            console.info('[Strategy] CE Sell already open for strike', ceStrike, '— skipped.');
        }

        if (!alreadySold(expiry, peStrike, 'Put')) {
            optionLegs.push({
                type:       'Sell',
                optionType: 'Put',
                strike:     peStrike,
                premium:    pePremium,
                quantity:   65,
                expiry:     expiry,
                entryDate:  chainTs
            });
            added = true;
        } else {
            console.info('[Strategy] PE Sell already open for strike', peStrike, '— skipped.');
        }

        // ── 8. refresh all UI panels ─────────────────────────────────────
        if (added) {
            refreshUI();
            // Freeze adjustment levels after chart has rendered (new Sell positions opened)
            setTimeout(function () {
                if (typeof global.freezeAdjustmentLevels === 'function') {
                    global.freezeAdjustmentLevels();
                }
            }, 300);
        }
    }

    // ─── strategy dispatcher ──────────────────────────────────────────────

    function runStrategy() {
        var stratEl  = document.getElementById('strategySelector');
        var expiryEl = document.getElementById('strategyExpirySelector');
        var strategy = stratEl  ? stratEl.value  : 'mini_strangle';
        var expiry   = expiryEl ? expiryEl.value : 'CURRENT_WEEK';

        switch (strategy) {
            case 'mini_strangle':
                executeMiniStrangle(expiry);
                break;
            default:
                console.warn('[Strategy] Unknown strategy:', strategy);
        }
    }

    // ─── Next Adjustment Deduct ───────────────────────────────────────────

    /** Maps TCP unit string to API numeric type (1 = points, 2 = percentage). */
    function unitToType(unit) {
        return unit === 'pct' ? 2 : 1;
    }

    /** Maps expiry selector value to lowercase snake_case for API. */
    function mapExpiry(v) {
        return (v || 'CURRENT_WEEK').toLowerCase();   // e.g. "current_week"
    }

    /**
     * Renders the API response inside the result modal and opens it.
     * Handles both the event-triggered case and the no-event case.
     */
    function showAdjDeductResult(data) {
        var modal = document.getElementById('nadModal');
        var body  = document.getElementById('nadModalBody');
        if (!modal || !body) return;

        var html = '';

        if (data && data.error) {
            html = '<div style="color:#ef4444; font-size:13px;">Error: ' + data.error + '</div>';

        } else if (data && data.event_triggered) {
            // ── Event was triggered ─────────────────────────────────────
            var color = '#3b82f6';
            html  = row('Event',          data.event_type        || '—');
            html += row('Triggered at',   data.trigger_timestamp || '—');
            html += row('Validate from',  data.validation_start_timestamp || '—',
                        '(5 min before trigger)');

        } else {
            // ── No event triggered ──────────────────────────────────────
            html  = '<div style="background:#f0fdf4; border:1px solid #bbf7d0; border-radius:6px; '
                  + 'padding:10px 14px; color:#16a34a; font-size:13px; font-weight:600; margin-bottom:12px;">'
                  + (data && data.message ? data.message : 'No event triggered') + '</div>';
            if (data && data.position_end_timestamp) {
                html += row('Position End Time', data.position_end_timestamp);
            }
        }

        body.innerHTML = html;
        modal.style.display = 'flex';
    }

    /** Helper — renders one labeled row in the result card. */
    function row(label, value, note) {
        return '<div style="display:flex; justify-content:space-between; align-items:baseline; '
             + 'padding:6px 0; border-bottom:1px solid #f1f5f9;">'
             + '<span style="font-size:11px; font-weight:700; color:#64748b; text-transform:uppercase; '
             + 'letter-spacing:.5px; white-space:nowrap; margin-right:12px;">' + label + '</span>'
             + '<span style="font-size:13px; color:#1e293b; text-align:right;">'
             + value
             + (note ? '<br><span style="font-size:10px; color:#94a3b8;">' + note + '</span>' : '')
             + '</span></div>';
    }

    /**
     * Reads all TCP settings + frozen adjustment levels, builds the payload,
     * and fires POST /next-adjustment-deduct.
     */
    function runNextAdjDeduct() {
        var settings  = typeof global.tcpGetSettings === 'function' ? global.tcpGetSettings() : {};
        var adjLevels = typeof global._getAdjLevels  === 'function' ? global._getAdjLevels()  : null;
        var ts        = typeof global._ocGetTimestamp === 'function' ? global._ocGetTimestamp() : new Date().toISOString();

        var sl  = settings.stopLoss   || {};
        var tgt = settings.target     || {};
        var tsl = settings.trailSL    || {};
        var tc  = settings.timeControl || {};

        var stratEl  = document.getElementById('strategySelector');
        var expiryEl = document.getElementById('strategyExpirySelector');

        // Serialise open positions (convert 'Call'/'Put' → 'CE'/'PE' for the backend)
        var openPositions = (typeof optionLegs !== 'undefined' ? optionLegs : [])
            .filter(function (leg) { return !leg.exited; })
            .map(function (leg) {
                return {
                    leg_type:      leg.type,
                    option_type:   leg.optionType === 'Call' ? 'CE' : 'PE',
                    strike:        leg.strike,
                    expiry:        leg.expiry,
                    entry_premium: leg.premium,
                    quantity:      leg.quantity
                };
            });

        var closedPnl = typeof global._getClosedPositionsPnl === 'function'
            ? global._getClosedPositionsPnl()
            : 0;

        var payload = {
            timestamp:              ts,
            upper_adjustment_point: adjLevels ? adjLevels.upper : null,
            lower_adjustment_point: adjLevels ? adjLevels.lower : null,
            lot:                    settings.lots || 2,
            lot_size:               65,
            timeframe:              settings.timeframe || '1m',
            strategy_type:          stratEl  ? stratEl.value  : 'mini_strangle',
            expiry_type:            mapExpiry(expiryEl ? expiryEl.value : 'CURRENT_WEEK'),
            stoploss_status:        sl.enabled  ? 1 : 0,
            stoploss_type:          unitToType(sl.unit),
            stoploss_value:         sl.value  || 0,
            target_status:          tgt.enabled ? 1 : 0,
            target_type:            unitToType(tgt.unit),
            target_value:           tgt.value || 0,
            trailing_sl_status:     tsl.enabled ? 1 : 0,
            trailing_sl_type:       unitToType(tsl.unit),
            trailing_sl_x:          tsl.x || 0,
            trailing_sl_y:          tsl.y || 0,
            position_end_time:      tc.exitTime || '15:26',
            open_positions:         openPositions,
            closed_positions_pnl:   closedPnl
        };

        console.info('[NextAdjDeduct] Payload:', payload);

        var btn = document.getElementById('nextAdjDeductBtn');
        if (btn) { btn.disabled = true; btn.textContent = 'Loading…'; }

        fetch('http://0.0.0.0:8000/next-adjustment-deduct', {
            method:  'POST',
            headers: { 'Content-Type': 'application/json' },
            body:    JSON.stringify(payload)
        })
        .then(function (res) {
            if (!res.ok) throw new Error('HTTP ' + res.status);
            return res.json();
        })
        .then(function (data) {
            showAdjDeductResult(data);
        })
        .catch(function (err) {
            console.error('[NextAdjDeduct] Request failed:', err);
            showAdjDeductResult({ error: err.message });
        })
        .then(function () {
            // always re-enable button (acts as .finally for older engines)
            if (btn) { btn.disabled = false; btn.textContent = 'Next Adjustment Deduct'; }
        });
    }

    // ─── DOM wiring ───────────────────────────────────────────────────────

    function init() {
        // Wire the main strategy_button
        var btn = document.querySelector('button.strategy_button');
        if (btn) {
            btn.addEventListener('click', function (e) {
                e.preventDefault();
                e.stopPropagation();
                runStrategy();
            });
        }

        // Wire Next Adjustment Deduct button
        var nadBtn = document.getElementById('nextAdjDeductBtn');
        if (nadBtn) {
            nadBtn.addEventListener('click', function (e) {
                e.preventDefault();
                e.stopPropagation();
                runNextAdjDeduct();
            });
        }

        // Wire modal close button + backdrop click
        var modalClose = document.getElementById('nadModalClose');
        if (modalClose) {
            modalClose.addEventListener('click', function () {
                var modal = document.getElementById('nadModal');
                if (modal) modal.style.display = 'none';
            });
        }
        var nadModal = document.getElementById('nadModal');
        if (nadModal) {
            nadModal.addEventListener('click', function (e) {
                if (e.target === nadModal) nadModal.style.display = 'none';
            });
        }
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

})(window);
