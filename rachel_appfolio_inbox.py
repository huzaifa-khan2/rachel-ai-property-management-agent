"""
Rachel - AppFolio Communication Inbox Monitor
Uses AppFolio Communication → Inbox as a lightweight event feed for tenant,
vendor, owner, property, and maintenance communications. List rows are
compared with durable state before any thread is opened.

Runs on its own configurable cadence through the shared browser manager.
"""

import os, re, json, time, random, sqlite3, subprocess, hashlib
from datetime import datetime
from urllib.parse import parse_qs, unquote, urlsplit
from anthropic import Anthropic
from rachel_appfolio_ui import AppFolioReadAdapter
from rachel_foundation_health import assert_zero_write_metrics
from rachel_event_memory import (
    backfill_conversation_identity_for_legacy_thread,
    conversation_identity_key,
    conversation_state_key,
    find_related_work_order,
    inbox_event_key,
    inbox_selector_key,
    initialize_event_memory,
    record_communication_state,
    resolve_and_link_conversation_identity,
    store_communication,
    unseen_communication_rows,
)
from rachel_communication_workflow import (
    CommunicationWorkflowError,
    classify_communication_ownership,
    create_response_draft_plan,
    generate_response_draft,
    store_response_draft,
)

def get_secret(env_name, service):
    value = os.getenv(env_name, "")
    if value:
        return value
    try:
        return subprocess.check_output(
            [
                "security",
                "find-generic-password",
                "-a",
                "rachel",
                "-s",
                service,
                "-w",
            ],
            text=True,
        ).strip()
    except Exception:
        return ""

APPFOLIO_URL = os.getenv("APPFOLIO_URL",
                         "https://clientco.appfolio.com")
APPFOLIO_EMAIL = get_secret("APPFOLIO_EMAIL", "APPFOLIO_EMAIL")
APPFOLIO_PASS = get_secret("APPFOLIO_PASSWORD", "APPFOLIO_PASSWORD")
DB_PATH = os.getenv("RACHEL_DB_PATH", "/tmp/rachel_data.db")
INBOX_URL = f"{APPFOLIO_URL}/texting/inbox"

client = Anthropic()

# Reuse helpers from triage module
from rachel_wo_triage import (
    send_sms_via_gmail, login_appfolio, human_delay,
    has_enough_info, get_db, CLOSING_MESSAGE,
    get_suggested_vendors, format_vendor_sms,
    _save_triage_state, analyze_photos,
    _truncate_at_word_boundary,
)


def _legacy_check_appfolio_inbox(page) -> int:
    """
    Navigate to AppFolio Communication → Inbox, find unread messages,
    read each one, match to active WO triage, and continue the conversation.

    Args:
        page: an already-logged-in Playwright page

    Returns:
        number of messages processed
    """
    raise RuntimeError(
        "Legacy unread-row inbox crawling is disabled; use check_appfolio_inbox."
    )
    print(f"\n[AF-Inbox] Checking AppFolio texting inbox...")
    page.goto(INBOX_URL, wait_until="domcontentloaded", timeout=30000)
    human_delay(3, 5)

    # Find unread messages — they have a blue dot indicator
    # Each row in the inbox is a conversation thread
    unread_rows = page.evaluate("""
        () => {
            const rows = document.querySelectorAll(
                'tr, div[class*="conversation"], div[class*="message-row"], '
                + 'a[class*="conversation"], li[class*="conversation"]');
            const unreads = [];
            for (const row of rows) {
                // Check for unread indicator (blue dot, bold text, unread class)
                const hasUnread = row.querySelector('.unread, .badge, [class*="unread"]')
                    || row.classList.contains('unread')
                    || row.querySelector('span[style*="color: blue"], .dot');
                // Also check for bold text which indicates unread
                const isBold = window.getComputedStyle(
                    row.querySelector('span, div, td') || row
                ).fontWeight >= 600;

                if (hasUnread || isBold) {
                    const text = row.textContent.trim();
                    // Skip if it's a system message or from Rachel
                    if (text.includes('Rachel Asst') && !text.includes('Received')) {
                        continue;
                    }
                    // Extract sender info
                    const link = row.querySelector('a[href*="/texting/inbox/"]');
                    const href = link ? link.getAttribute('href') : '';
                    unreads.push({
                        text: text.substring(0, 200),
                        href: href
                    });
                }
            }
            return unreads;
        }
    """)

    if not unread_rows:
        # Simpler approach: just find all clickable inbox items and check for unread indicators
        unread_rows = page.evaluate("""
            () => {
                const results = [];
                // Look for the blue dot (●) character which indicates unread
                const allText = document.body.innerText;
                const links = document.querySelectorAll('a[href*="/texting/inbox/"]');
                for (const link of links) {
                    const row = link.closest('tr, div, li') || link;
                    const text = row.textContent.trim();
                    // Unread messages often have ● or bold styling
                    if (text.includes('●') || text.includes('Received')) {
                        results.push({
                            text: text.substring(0, 200),
                            href: link.getAttribute('href')
                        });
                    }
                }
                return results;
            }
        """)

    if not unread_rows:
        print("[AF-Inbox] No unread messages")
        return 0

    print(f"[AF-Inbox] Found {len(unread_rows)} potential unread message(s)")

    processed = 0
    for row in unread_rows:
        href = row.get("href", "")
        preview = row.get("text", "")[:100]

        if not href:
            continue

        # Navigate to the message thread
        full_url = href if href.startswith("http") else APPFOLIO_URL + href
        print(f"[AF-Inbox] Opening thread: {preview[:60]}")
        page.goto(full_url, wait_until="domcontentloaded", timeout=30000)
        human_delay(2, 4)

        # Extract the conversation from the thread page
        thread_data = page.evaluate("""
            () => {
                const messages = [];
                // Find all message bubbles
                const bubbles = document.querySelectorAll(
                    '[class*="message"], [class*="bubble"], '
                    + 'div[class*="sms"], div[class*="text-message"]');

                for (const b of bubbles) {
                    const text = b.textContent.trim();
                    if (text.length < 5) continue;
                    // Determine if sent or received
                    const isSent = b.classList.contains('sent')
                        || b.classList.contains('outgoing')
                        || b.querySelector('[class*="sent"]') !== null
                        || text.includes('Rachel Asst');
                    messages.push({
                        text: text.substring(0, 500),
                        sent: isSent
                    });
                }

                // Get sender info from the header
                const header = document.querySelector(
                    'h1, h2, h3, [class*="header"], [class*="contact"]');
                const headerText = header ? header.textContent.trim() : '';

                // Get phone number from page
                const phoneEl = document.querySelector(
                    'a[href*="tel:"], [class*="phone"]');
                const phone = phoneEl ? phoneEl.textContent.trim() : '';

                return {
                    sender: headerText.substring(0, 100),
                    phone: phone,
                    messages: messages
                };
            }
        """)

        sender = thread_data.get("sender", "Unknown")
        phone = thread_data.get("phone", "")
        messages = thread_data.get("messages", [])

        if not messages:
            # Fallback: read the page text more broadly
            page_text = page.evaluate("""
                () => {
                    const main = document.querySelector('main, [role="main"], .content')
                        || document.body;
                    return main.innerText.substring(0, 2000);
                }
            """)
            # Find the last "Received" message
            received_match = re.findall(
                r"(.+?)\s*Received\s*-\s*\w+\s+\d+,\s+\d+\s+\d+:\d+\s+[AP]M",
                page_text)
            if received_match:
                tenant_reply = received_match[-1].strip()
                print(f"[AF-Inbox] Tenant reply (fallback): {tenant_reply[:80]}")
            else:
                print(f"[AF-Inbox] Could not extract messages from thread")
                continue
        else:
            # Find the last received (not sent) message
            received = [m for m in messages if not m.get("sent")]
            if not received:
                print(f"[AF-Inbox] No received messages in thread")
                continue
            tenant_reply = received[-1]["text"]
            # Clean up — remove timestamps and labels
            tenant_reply = re.sub(
                r"Received\s*-\s*\w+\s+\d+,\s+\d+.*$", "",
                tenant_reply).strip()
            print(f"[AF-Inbox] Tenant reply: {tenant_reply[:80]}")

        # Clean sender name
        sender_name = re.sub(
            r"\s*(OWNER|TENANT|VENDOR)\s*$", "", sender).strip()
        sender_name = re.sub(r"\s*\(?\d{3}\)?[-.\s]\d{3}.*$", "",
                             sender_name).strip()
        print(f"[AF-Inbox] From: {sender_name} | Phone: {phone}")

        # Match to an active WO triage in the database
        conn = get_db()
        # Try matching by tenant name
        wo_row = conn.execute(
            "SELECT * FROM wo_triage WHERE tenant LIKE ? "
            "AND status IN ("
            "'awaiting_reply', "
            "'waiting_for_tenant_availability', "
            "'waiting_for_tenant_confirmation', "
            "'satisfaction_check_sent'"
            ") "
            "ORDER BY updated_at DESC LIMIT 1",
            (f"%{sender_name.split()[-1] if sender_name else '???'}%",)
        ).fetchone()
        conn.close()

        if not wo_row:
            print(f"[AF-Inbox] No active triage for {sender_name}")
            # Notify Owner/Manager about unmatched message
            send_sms_via_gmail(
                f"Unmatched tenant text in AppFolio inbox:\n"
                f"From: {sender_name} {phone}\n"
                f"Message: {_truncate_at_word_boundary(tenant_reply, 150)}",
                urgent=False)
            continue

        wo_num = wo_row["wo_num"]
        print(f"[AF-Inbox] Matched to WO#{wo_num}")

        # Route based on WO status
        if wo_row["status"] == "satisfaction_check_sent":
            try:
                from rachel_vendor_lifecycle import (
                    handle_tenant_satisfaction_reply)
                handle_tenant_satisfaction_reply(wo_num, tenant_reply)
                processed += 1
            except Exception as e:
                print(f"[AF-Inbox] Satisfaction handler error: {e}")
        elif wo_row["status"] == "awaiting_reply":
            # Continue triage conversation
            result = process_triage_reply(
                page, wo_row, tenant_reply)
            if result:
                processed += 1

        elif wo_row["status"] in (
            "waiting_for_tenant_availability",
            "waiting_for_tenant_confirmation",
        ):
            # Continue the vendor/tenant appointment coordination workflow.
            try:
                from rachel_vendor_lifecycle import (
                    handle_tenant_schedule_reply,
                )

                handle_tenant_schedule_reply(
                    wo_num,
                    tenant_reply,
                )
                processed += 1
            except Exception as error:
                print(
                    f"[AF-Inbox] Scheduling reply handler error "
                    f"for WO#{wo_num}: {error}"
                )

    print(f"[AF-Inbox] Processed {processed} message(s)")
    return processed


def appfolio_thread_id_from_href(href: str) -> str:
    """Return the thread segment from relative or absolute AppFolio URLs."""
    try:
        parsed = urlsplit(str(href or "").strip())
    except (TypeError, ValueError):
        return ""
    path = parsed.path
    segments = [segment for segment in path.split("/") if segment]
    for index in range(len(segments) - 2):
        if [part.lower() for part in segments[index:index + 2]] == [
            "texting", "inbox"
        ]:
            return unquote(segments[index + 2]).strip()
    if "/texting/inbox" not in path.lower():
        return ""
    query = parse_qs(parsed.query)
    for key in (
        "conversation_id", "conversationId", "thread_id", "threadId"
    ):
        value = (query.get(key) or [""])[0]
        if value:
            return unquote(value).strip()
    return ""


_INBOX_FRAME_DIAGNOSTICS_SCRIPT = r"""
    () => {
        const selectors = {
            href_inbox: 'a[href*="/texting/inbox"]',
            data_href_inbox: '[data-href*="/texting/inbox"]',
            data_url_inbox: '[data-url*="/texting/inbox"]',
            conversation_id: '[data-conversation-id]',
            thread_id: '[data-thread-id]',
            conversation_class: '[class*="conversation"]',
            message_row_class: '[class*="message-row"]',
            semantic_rows: 'tr, [role="row"], [role="listitem"]',
            appfolio_list_rows: 'ul.list-group > button > li.list-group-item',
            iframe_elements: 'iframe'
        };
        const hits = {};
        for (const [name, selector] of Object.entries(selectors)) {
            hits[name] = document.querySelectorAll(selector).length;
        }
        hits.candidate_rows = document.querySelectorAll([
            selectors.href_inbox,
            selectors.data_href_inbox,
            selectors.data_url_inbox,
            selectors.conversation_id,
            selectors.thread_id
        ].join(',')).length;
        return hits;
    }
"""


_INBOX_ROW_STRUCTURE_DIAGNOSTIC_SCRIPT = r"""
    () => {
        // SANITIZED_INBOX_ROW_STRUCTURE_DIAGNOSTIC
        // This diagnostic never clicks and never returns DOM text or raw
        // attribute values. Text nodes become positional placeholders and
        // attribute values become per-report tokens plus structural shapes.
        const round = value => Math.round(Number(value || 0));
        const viewport = {
            width: round(window.innerWidth),
            height: round(window.innerHeight)
        };
        const valueTokens = new Map();
        let nextValueToken = 1;

        const tokenFor = raw => {
            const value = String(raw || '');
            if (!valueTokens.has(value)) {
                valueTokens.set(value, `VALUE_${nextValueToken++}`);
            }
            return valueTokens.get(value);
        };

        const valueKind = value => {
            if (!value) return 'empty';
            if (/^(?:true|false)$/i.test(value)) return 'boolean';
            if (/^\d+$/.test(value)) return 'numeric';
            if (/^[0-9a-f]{8}-[0-9a-f-]{27,}$/i.test(value)) return 'uuid';
            if (/^(?:https?:\/\/|\/)/i.test(value)) return 'navigation';
            if (/^[A-Za-z_][A-Za-z0-9_.:-]*$/.test(value)) return 'identifier';
            return 'opaque';
        };

        const routeShape = raw => {
            const value = String(raw || '');
            if (!/^(?:https?:\/\/|\/)/i.test(value)) return '';
            try {
                const parsed = new URL(value, window.location.origin);
                const safeSegments = new Set([
                    'texting', 'inbox', 'communication', 'communications',
                    'maintenance', 'service_requests', 'work_orders',
                    'people', 'properties', 'owners', 'tenants', 'vendors'
                ]);
                const shaped = parsed.pathname.split('/').map(segment => {
                    if (!segment) return '';
                    return safeSegments.has(segment.toLowerCase())
                        ? segment.toLowerCase()
                        : ':dynamic';
                }).join('/');
                const queryKeys = Array.from(parsed.searchParams.keys())
                    .sort()
                    .map(() => ':query_key');
                return shaped + (queryKeys.length ? `?${queryKeys.join('&')}` : '');
            } catch (_error) {
                return 'unparseable_navigation';
            }
        };

        const summarizeValue = raw => {
            const value = String(raw || '');
            const summary = {
                value_token: tokenFor(value),
                value_kind: valueKind(value),
                value_length: value.length
            };
            const shape = routeShape(value);
            if (shape) summary.route_shape = shape;
            return summary;
        };

        const isVisible = element => {
            if (!(element instanceof Element)) return false;
            const rect = element.getBoundingClientRect();
            if (rect.width < 1 || rect.height < 1) return false;
            const style = window.getComputedStyle(element);
            return style.display !== 'none' &&
                style.visibility !== 'hidden' &&
                style.opacity !== '0';
        };

        const rectFor = (element, relativeTo = null) => {
            const rect = element.getBoundingClientRect();
            const base = relativeTo
                ? relativeTo.getBoundingClientRect()
                : {left: 0, top: 0};
            return {
                x: round(rect.left - base.left),
                y: round(rect.top - base.top),
                width: round(rect.width),
                height: round(rect.height)
            };
        };

        const allElements = [];
        const collectElements = element => {
            allElements.push(element);
            for (const child of Array.from(element.children || [])) {
                collectElements(child);
            }
            if (element.shadowRoot) {
                for (const child of Array.from(element.shadowRoot.children || [])) {
                    collectElements(child);
                }
            }
        };
        if (document.documentElement) collectElements(document.documentElement);
        const composedParent = element => {
            if (!element) return null;
            if (element.parentElement) return element.parentElement;
            const root = element.getRootNode ? element.getRootNode() : null;
            return root && root.host instanceof Element ? root.host : null;
        };
        const classFrequency = new Map();
        for (const element of allElements) {
            for (const token of Array.from(element.classList || [])) {
                classFrequency.set(token, (classFrequency.get(token) || 0) + 1);
            }
        }

        const safeClassTokens = element => Array.from(element.classList || [])
            .slice(0, 12)
            .map(token => {
                if (/@|\+|\d{7,}/.test(token) || token.length > 100) {
                    return '<REDACTED_DYNAMIC_CLASS>';
                }
                if ((classFrequency.get(token) || 0) < 2) {
                    return '<UNIQUE_CLASS_TOKEN>';
                }
                return token;
            });

        const structuralAttributes = element => {
            const results = [];
            for (const attribute of Array.from(element.attributes || [])) {
                const name = attribute.name.toLowerCase();
                const relevant = name === 'id' ||
                    name === 'href' ||
                    name === 'to' ||
                    name === 'name' ||
                    name === 'tabindex' ||
                    name.startsWith('data-') ||
                    name.startsWith('aria-') ||
                    name.startsWith('on');
                if (!relevant || name === 'class') continue;
                results.push({
                    attribute_name: name,
                    ...summarizeValue(attribute.value)
                });
            }
            return results.slice(0, 24);
        };

        const reactStructure = element => {
            const result = {
                detected: false,
                internal_key_types: [],
                handler_prop_names: [],
                identity_prop_candidates: []
            };
            for (const internalKey of Object.keys(element)) {
                if (!/^__(?:react|preact)/i.test(internalKey)) continue;
                result.detected = true;
                result.internal_key_types.push(
                    internalKey.replace(/\$.*/, '$<REDACTED>')
                );
                if (!/^__reactProps\$/i.test(internalKey)) continue;
                const props = element[internalKey];
                if (!props || typeof props !== 'object') continue;
                for (const [propName, propValue] of Object.entries(props)) {
                    if (/^on[A-Z]/.test(propName) && typeof propValue === 'function') {
                        result.handler_prop_names.push(propName);
                        continue;
                    }
                    if (!/(?:conversation|thread|message|inbox|href|route|path|target|\bid$)/i.test(propName)) {
                        continue;
                    }
                    if (!['string', 'number', 'boolean'].includes(typeof propValue)) {
                        result.identity_prop_candidates.push({
                            prop_name: propName,
                            value_kind: typeof propValue
                        });
                        continue;
                    }
                    result.identity_prop_candidates.push({
                        prop_name: propName,
                        ...summarizeValue(propValue)
                    });
                }
            }
            result.internal_key_types = Array.from(
                new Set(result.internal_key_types)
            ).slice(0, 8);
            result.handler_prop_names = Array.from(
                new Set(result.handler_prop_names)
            ).slice(0, 12);
            result.identity_prop_candidates = (
                result.identity_prop_candidates.slice(0, 16)
            );
            return result;
        };

        const directCharacterCount = element => Array.from(element.childNodes)
            .filter(node => node.nodeType === Node.TEXT_NODE)
            .reduce((total, node) => total + String(node.nodeValue || '').trim().length, 0);

        const describeElement = (element, relativeTo = null) => {
            const style = window.getComputedStyle(element);
            return {
                tag_name: element.tagName.toLowerCase(),
                class_tokens: safeClassTokens(element),
                role: element.getAttribute('role') || '',
                geometry: rectFor(element, relativeTo),
                display: style.display,
                position: style.position,
                cursor: style.cursor,
                border_bottom_style: style.borderBottomStyle,
                border_bottom_width: style.borderBottomWidth,
                direct_child_elements: element.children.length,
                descendant_elements: element.querySelectorAll('*').length,
                direct_text_placeholder: directCharacterCount(element) > 0
                    ? {
                        placeholder: '<TEXT_REDACTED>',
                        character_count: directCharacterCount(element)
                    }
                    : null,
                structural_attributes: structuralAttributes(element),
                react_structure: reactStructure(element)
            };
        };

        const textSlots = row => {
            const slots = [];
            const candidates = [row, ...Array.from(row.querySelectorAll('*'))];
            for (const element of candidates) {
                if (!isVisible(element)) continue;
                const characterCount = directCharacterCount(element);
                if (!characterCount) continue;
                slots.push({
                    placeholder: `<TEXT_SLOT_${slots.length + 1}>`,
                    character_count: characterCount,
                    tag_name: element.tagName.toLowerCase(),
                    class_tokens: safeClassTokens(element),
                    geometry: rectFor(element, row)
                });
                if (slots.length >= 20) break;
            }
            return slots;
        };

        const identityCandidates = row => {
            const results = [];
            const candidates = [row, ...Array.from(row.querySelectorAll('*'))]
                .slice(0, 120);
            for (let index = 0; index < candidates.length; index += 1) {
                const element = candidates[index];
                const attributes = structuralAttributes(element).filter(item =>
                    item.attribute_name === 'id' ||
                    item.attribute_name === 'href' ||
                    item.attribute_name === 'to' ||
                    item.attribute_name.startsWith('data-') ||
                    item.attribute_name === 'aria-controls' ||
                    item.attribute_name === 'aria-labelledby'
                );
                const react = reactStructure(element);
                if (!attributes.length &&
                    !react.handler_prop_names.length &&
                    !react.identity_prop_candidates.length) {
                    continue;
                }
                results.push({
                    descendant_index: index,
                    tag_name: element.tagName.toLowerCase(),
                    class_tokens: safeClassTokens(element),
                    structural_attributes: attributes,
                    react_structure: react
                });
                if (results.length >= 30) break;
            }
            return results;
        };

        const unreadDotCandidates = row => Array.from(row.querySelectorAll('*'))
            .filter(element => {
                if (!isVisible(element)) return false;
                const rect = element.getBoundingClientRect();
                if (rect.width > 24 || rect.height > 24) return false;
                const style = window.getComputedStyle(element);
                const classHint = Array.from(element.classList || [])
                    .some(token => /unread|dot|badge|status/i.test(token));
                const roundShape = parseFloat(style.borderRadius || '0') >=
                    Math.min(rect.width, rect.height) * 0.4;
                const colored = !['rgba(0, 0, 0, 0)', 'transparent', 'rgb(255, 255, 255)']
                    .includes(style.backgroundColor);
                return classHint || (roundShape && colored);
            })
            .slice(0, 12)
            .map(element => ({
                ...describeElement(element, row),
                visual_hint: '<POSSIBLE_UNREAD_DOT>'
            }));

        const dateCandidates = row => Array.from(row.querySelectorAll('*'))
            .filter(element => {
                if (!isVisible(element)) return false;
                const rect = element.getBoundingClientRect();
                const rowRect = row.getBoundingClientRect();
                const classHint = Array.from(element.classList || [])
                    .some(token => /date|day|time|timestamp/i.test(token));
                const semanticHint = element.tagName.toLowerCase() === 'time' ||
                    element.hasAttribute('datetime');
                const rightEdgeHint = rect.left >= rowRect.left + rowRect.width * 0.75 &&
                    directCharacterCount(element) > 0 &&
                    directCharacterCount(element) <= 32;
                return classHint || semanticHint || rightEdgeHint;
            })
            .slice(0, 12)
            .map(element => ({
                ...describeElement(element, row),
                visual_hint: '<POSSIBLE_DATE_OR_DAY>'
            }));

        const descendantStructure = row => {
            const results = [];
            const visit = (element, parentIndex, depth) => {
                if (depth > 3 || results.length >= 60) return;
                const index = results.length;
                results.push({
                    node_index: index,
                    parent_node_index: parentIndex,
                    depth: depth,
                    ...describeElement(element, row)
                });
                for (const child of Array.from(element.children)) {
                    if (isVisible(child)) visit(child, index, depth + 1);
                    if (results.length >= 60) break;
                }
            };
            visit(row, -1, 0);
            return results;
        };

        const describeRow = (row, childIndex) => ({
            child_index: childIndex,
            element: describeElement(row),
            text_slots: textSlots(row),
            identity_candidates: identityCandidates(row),
            unread_dot_candidates: unreadDotCandidates(row),
            date_candidates: dateCandidates(row),
            icon_count: Array.from(row.querySelectorAll('svg, i, [role="img"]'))
                .filter(isVisible).length,
            descendant_structure: descendantStructure(row)
        });

        const visibleElements = allElements.filter(isVisible);
        const visibleIndex = new Map(
            visibleElements.map((element, index) => [element, index])
        );
        const frequencyMap = items => {
            const counts = {};
            for (const item of items) counts[item] = (counts[item] || 0) + 1;
            return Object.entries(counts)
                .sort((left, right) => right[1] - left[1])
                .slice(0, 30)
                .map(([name, count]) => ({name, count}));
        };

        const standardRoles = new Set([
            'alert', 'button', 'checkbox', 'combobox', 'dialog', 'grid',
            'gridcell', 'group', 'img', 'link', 'list', 'listbox',
            'listitem', 'main', 'menu', 'menuitem', 'navigation', 'option',
            'progressbar', 'radio', 'region', 'row', 'rowgroup', 'search',
            'separator', 'status', 'switch', 'tab', 'table', 'tabpanel',
            'textbox', 'toolbar', 'tooltip', 'tree', 'treeitem'
        ]);
        const safeRole = element => {
            const role = String(element.getAttribute('role') || '').toLowerCase();
            if (!role) return '';
            return standardRoles.has(role) ? role : '<NONSTANDARD_ROLE>';
        };
        const reusableClasses = element => safeClassTokens(element)
            .filter(token => token !== '<UNIQUE_CLASS_TOKEN>');
        const classSemanticHints = element => {
            const hints = new Set();
            for (const token of Array.from(element.classList || [])) {
                for (const hint of [
                    'active', 'badge', 'conversation', 'date', 'dot', 'item',
                    'list', 'message', 'preview', 'read', 'selected', 'snippet',
                    'status', 'time', 'unread'
                ]) {
                    if (token.toLowerCase().includes(hint)) hints.add(hint);
                }
            }
            return Array.from(hints).sort();
        };
        const safeAttributeNames = element => structuralAttributes(element)
            .map(item => item.attribute_name)
            .filter(name => !/@|\+|\d{7,}/.test(name) && name.length <= 100)
            .sort();
        const signatureParts = element => ({
            tag_name: element.tagName.toLowerCase(),
            class_tokens: reusableClasses(element),
            role: safeRole(element),
            structural_attribute_names: safeAttributeNames(element),
            direct_child_tags: Array.from(element.children)
                .filter(isVisible)
                .map(child => child.tagName.toLowerCase())
                .slice(0, 30)
        });
        const signatureKey = element => JSON.stringify(signatureParts(element));
        const signatureToken = element => tokenFor(
            `STRUCTURE:${signatureKey(element)}`
        );
        const isClickable = element => {
            const role = safeRole(element);
            const tag = element.tagName.toLowerCase();
            const react = reactStructure(element);
            return ['a', 'button'].includes(tag) ||
                ['button', 'link', 'row', 'listitem'].includes(role) ||
                element.hasAttribute('tabindex') ||
                window.getComputedStyle(element).cursor === 'pointer' ||
                react.handler_prop_names.includes('onClick');
        };

        // Record every visible element before making any row/list assumptions.
        const elementInventory = visibleElements.map((element, index) => {
            const parent = composedParent(element);
            const grandparent = composedParent(parent);
            const react = reactStructure(element);
            const directTextCount = directCharacterCount(element);
            const style = window.getComputedStyle(element);
            return {
                element_index: index,
                parent_element_index: visibleIndex.has(parent)
                    ? visibleIndex.get(parent) : -1,
                grandparent_element_index: visibleIndex.has(grandparent)
                    ? visibleIndex.get(grandparent) : -1,
                structural_signature_token: signatureToken(element),
                parent_structural_signature_token: parent
                    ? signatureToken(parent) : '',
                grandparent_structural_signature_token: grandparent
                    ? signatureToken(grandparent) : '',
                ...signatureParts(element),
                class_semantic_hints: classSemanticHints(element),
                geometry: rectFor(element),
                display: style.display,
                position: style.position,
                visual_state: {
                    font_weight: style.fontWeight,
                    background_color: style.backgroundColor,
                    foreground_color: style.color,
                    border_bottom_style: style.borderBottomStyle,
                    border_bottom_width: style.borderBottomWidth
                },
                direct_child_count: element.children.length,
                visible_direct_child_count: Array.from(element.children)
                    .filter(isVisible).length,
                descendant_element_count: element.querySelectorAll('*').length,
                sibling_index: parent
                    ? Array.from(parent.children).indexOf(element) : -1,
                clickable: isClickable(element),
                text_placeholder: directTextCount > 0
                    ? {
                        placeholder: '<TEXT_REDACTED>',
                        character_count: directTextCount
                    }
                    : null,
                react_structure: {
                    detected: react.detected,
                    internal_key_types: react.internal_key_types,
                    handler_prop_names: react.handler_prop_names,
                    identity_prop_names: react.identity_prop_candidates
                        .map(item => item.prop_name)
                }
            };
        });

        // Group only after the complete inventory exists.  This is descriptive:
        // no minimum geometry, semantic role, href, or conversation hint is used.
        const signatureBuckets = new Map();
        for (const item of elementInventory) {
            const token = item.structural_signature_token;
            if (!signatureBuckets.has(token)) signatureBuckets.set(token, []);
            signatureBuckets.get(token).push(item);
        }
        const repeatedSignatureGroups = Array.from(signatureBuckets.entries())
            .filter(([_token, items]) => items.length >= 2)
            .map(([token, items]) => ({
                structural_signature_token: token,
                count: items.length,
                signature: signatureParts(visibleElements[items[0].element_index]),
                element_indexes: items.map(item => item.element_index),
                bounding_boxes: items.map(item => item.geometry)
            }))
            .sort((left, right) => right.count - left.count);

        const siblingBuckets = new Map();
        for (const item of elementInventory) {
            if (item.parent_element_index < 0) continue;
            const key = `${item.parent_element_index}|${item.structural_signature_token}`;
            if (!siblingBuckets.has(key)) siblingBuckets.set(key, []);
            siblingBuckets.get(key).push(item);
        }
        const verticalSiblingStacks = [];
        for (const items of siblingBuckets.values()) {
            if (items.length < 2) continue;
            const ordered = [...items].sort(
                (left, right) => left.geometry.y - right.geometry.y
            );
            const distinctY = new Set(ordered.map(item => item.geometry.y));
            if (distinctY.size < 2) continue;
            const gaps = [];
            for (let index = 1; index < ordered.length; index += 1) {
                gaps.push(round(
                    ordered[index].geometry.y -
                    (ordered[index - 1].geometry.y + ordered[index - 1].geometry.height)
                ));
            }
            verticalSiblingStacks.push({
                parent_element_index: ordered[0].parent_element_index,
                parent_structural_signature_token:
                    ordered[0].parent_structural_signature_token,
                child_structural_signature_token:
                    ordered[0].structural_signature_token,
                count: ordered.length,
                clickable_count: ordered.filter(item => item.clickable).length,
                element_indexes: ordered.map(item => item.element_index),
                bounding_boxes: ordered.map(item => item.geometry),
                vertical_gaps: gaps
            });
        }
        verticalSiblingStacks.sort((left, right) => right.count - left.count);

        const genericInventory = {
            tag_frequencies: frequencyMap(
                visibleElements.map(element => element.tagName.toLowerCase())
            ),
            role_frequencies: frequencyMap(
                visibleElements.map(element => element.getAttribute('role'))
                    .filter(Boolean)
            ),
            repeated_class_frequencies: frequencyMap(
                visibleElements.flatMap(element => safeClassTokens(element))
                    .filter(token => token !== '<UNIQUE_CLASS_TOKEN>')
            ),
            structural_attribute_name_frequencies: frequencyMap(
                visibleElements.flatMap(element =>
                    structuralAttributes(element).map(item => item.attribute_name)
                )
            ),
            clickable_element_count: visibleElements.filter(isClickable).length
        };
        return {
            schema_version: 3,
            sanitized: true,
            text_policy: '<ALL_DOM_TEXT_REPLACED_BY_POSITIONAL_PLACEHOLDERS>',
            attribute_policy: '<ALL_RAW_ATTRIBUTE_VALUES_REPLACED_BY_TOKENS>',
            viewport: viewport,
            visible_element_count: visibleElements.length,
            inventory_element_count: elementInventory.length,
            element_inventory: elementInventory,
            repeated_signature_group_count: repeatedSignatureGroups.length,
            repeated_signature_groups: repeatedSignatureGroups,
            vertical_sibling_stack_count: verticalSiblingStacks.length,
            vertical_sibling_stacks: verticalSiblingStacks,
            candidate_group_count: verticalSiblingStacks.length,
            candidate_groups: verticalSiblingStacks,
            generic_inventory: genericInventory
        };
    }
"""


_INBOX_LIGHTWEIGHT_ROWS_SCRIPT = r"""
        // AF_INBOX_LIGHTWEIGHT_ROWS
        () => {
            const results = [];
            const candidates = document.querySelectorAll(
                'ul.list-group > button'
            );
            const directChild = (element, selector) =>
                Array.from(element.children || []).find(child =>
                    child.matches(selector)
                ) || null;
            const cleanText = element =>
                element ? (element.innerText || element.textContent || '').trim() : '';
            const fontWeight = element => {
                if (!element) return 0;
                const raw = window.getComputedStyle(element).fontWeight;
                if (raw === 'bold') return 700;
                return Number.parseInt(raw, 10) || 0;
            };
            const pseudoVisible = element => {
                if (!element) return false;
                return ['::before', '::after'].some(pseudo => {
                    const style = window.getComputedStyle(element, pseudo);
                    const content = String(style.content || '');
                    const hasContent = !['', 'none', 'normal', '\"\"'].includes(content);
                    const background = String(style.backgroundColor || '');
                    const colored = background && ![
                        'rgba(0, 0, 0, 0)', 'transparent',
                        'rgb(255, 255, 255)'
                    ].includes(background);
                    return hasContent || colored;
                });
            };
            for (const candidate of candidates) {
                const row = directChild(candidate, 'li.list-group-item');
                if (!row) continue;
                const layout = directChild(row, 'div.d-flex');
                const indicator = layout
                    ? directChild(layout, 'div.d-flex.align-items-center')
                    : null;
                const contentColumn = layout
                    ? directChild(layout, 'div.d-flex.flex-column')
                    : null;
                if (!contentColumn) continue;
                const heading = directChild(
                    contentColumn, 'div.d-flex.justify-content-between'
                );
                const contactElement = heading
                    ? heading.querySelector('span[data-testid], span.text-truncate')
                    : null;
                const timestampElement = heading
                    ? heading.querySelector('small')
                    : null;
                const detailElement = directChild(
                    contentColumn, 'div.text-truncate'
                );
                const previewElement = directChild(
                    contentColumn, 'span.text-truncate'
                );
                const assignedElement = contentColumn.querySelector([
                    '[data-testid*="assignee"]',
                    '[data-testid*="assigned"]',
                    '[class*="assignee"]',
                    '[class*="assigned-user"]'
                ].join(','));
                const contact = cleanText(contactElement);
                const timestamp = cleanText(timestampElement);
                const detail = cleanText(detailElement);
                const preview = cleanText(previewElement);
                if (!contact || !preview) continue;
                const text = cleanText(row);
                const roleMatch = text.match(/\b(TENANT|VENDOR|OWNER|RESIDENT)\b/i);
                const classUnread = Boolean(
                    row.querySelector('.unread, .badge, [class*="unread"], .dot') ||
                    row.classList.contains('unread') ||
                    candidate.classList.contains('unread')
                );
                const ariaUnread = [candidate, row].some(element =>
                    /\bunread\b/i.test(element.getAttribute('aria-label') || '')
                );
                const boldUnread = Math.max(
                    fontWeight(contactElement), fontWeight(previewElement)
                ) >= 600;
                const indicatorUnread = Boolean(
                    indicator && (
                        indicator.querySelector('*') ||
                        cleanText(indicator) ||
                        pseudoVisible(indicator)
                    )
                );
                results.push({
                    source_message_id: '',
                    href: '',
                    contact_name: contact.substring(0, 160),
                    contact_role: roleMatch ? roleMatch[1].toLowerCase() : '',
                    property: '',
                    unit: '',
                    assigned_user: cleanText(assignedElement).substring(0, 160),
                    preview: preview.substring(0, 500),
                    timestamp: timestamp.substring(0, 120),
                    unread: classUnread || ariaUnread || boldUnread || indicatorUnread,
                    list_detail: detail.substring(0, 160),
                    identity_kind: 'list_metadata_sha256'
                });
            }
            return results;
        }
"""


_INBOX_FILTER_SCOPE_RESET_SCRIPT = r"""
    async () => {
        const rowSelector = 'ul.list-group > button > li.list-group-item';
        const rendered = element => {
            if (!(element instanceof Element)) return false;
            const rect = element.getBoundingClientRect();
            const style = window.getComputedStyle(element);
            return rect.width > 0 && rect.height > 0 &&
                style.display !== 'none' && style.visibility !== 'hidden' &&
                style.opacity !== '0';
        };
        const settle = () => new Promise(resolve => {
            window.requestAnimationFrame(() =>
                window.requestAnimationFrame(() =>
                    window.setTimeout(resolve, 250)
                )
            );
        });
        const safeFilterClick = element => {
            if (!(element instanceof HTMLButtonElement)) return false;
            if (!rendered(element) || element.disabled) return false;
            if (element.closest('ul.list-group')) return false;
            if (element.querySelector('li.list-group-item')) return false;
            element.click();
            return true;
        };
        const normalizedLabel = element => String(
            element.innerText || element.textContent || ''
        ).trim().toLowerCase().replace(/\s+/g, ' ');
        const clearLabels = new Set([
            'clear', 'reset', 'clear all', 'reset all', 'clear filters',
            'reset filters', 'clear all filters', 'remove filters'
        ]);
        const commitLabels = new Set([
            'apply', 'done', 'save', 'update'
        ]);
        const conversationList = Array.from(
            document.querySelectorAll('ul.list-group')
        ).find(list => list.querySelector(':scope > button > li.list-group-item'));
        if (!conversationList) {
            return {
                status: 'conversation_list_not_found',
                verified_full_scope: false,
                toolbar_found: false,
                dropdown_triggers_checked: 0,
                filter_controls_found: 0,
                filter_clear_actions: 0,
                filter_controls_already_clear: 0,
                commit_actions: 0,
                conversation_rows_before: 0,
                conversation_rows_after: 0,
                conversation_buttons_clicked: 0
            };
        }
        const listRect = conversationList.getBoundingClientRect();
        const toolbarCandidates = Array.from(
            document.querySelectorAll('[role="toolbar"]')
        ).filter(rendered).filter(toolbar => {
            const rect = toolbar.getBoundingClientRect();
            const horizontalOverlap = Math.max(
                0,
                Math.min(rect.right, listRect.right) -
                    Math.max(rect.left, listRect.left)
            );
            return horizontalOverlap > 0 && rect.bottom <= listRect.top + 5;
        }).sort((left, right) =>
            right.getBoundingClientRect().bottom -
                left.getBoundingClientRect().bottom
        );
        const toolbar = toolbarCandidates[0] || null;
        const beforeCount = document.querySelectorAll(rowSelector).length;
        if (!toolbar) {
            return {
                status: 'filter_toolbar_not_found',
                verified_full_scope: false,
                toolbar_found: false,
                dropdown_triggers_checked: 0,
                filter_controls_found: 0,
                filter_clear_actions: 0,
                filter_controls_already_clear: 0,
                commit_actions: 0,
                conversation_rows_before: beforeCount,
                conversation_rows_after: beforeCount,
                conversation_buttons_clicked: 0
            };
        }

        const initialTriggerCount = Array.from(
            toolbar.querySelectorAll('button[aria-haspopup]')
        ).filter(rendered).filter(button => !button.disabled).length;
        let triggersChecked = 0;
        let filterControlsFound = 0;
        let clearActions = 0;
        let alreadyClear = 0;
        let commitActions = 0;
        let unresolvedControls = 0;
        for (let index = 0; index < initialTriggerCount; index += 1) {
            const currentToolbar = toolbar.isConnected ? toolbar : Array.from(
                document.querySelectorAll('[role="toolbar"]')
            ).filter(rendered).filter(candidate => {
                const rect = candidate.getBoundingClientRect();
                const horizontalOverlap = Math.max(
                    0,
                    Math.min(rect.right, listRect.right) -
                        Math.max(rect.left, listRect.left)
                );
                return horizontalOverlap > 0 &&
                    rect.bottom <= listRect.top + 5;
            }).sort((left, right) =>
                right.getBoundingClientRect().bottom -
                    left.getBoundingClientRect().bottom
            )[0];
            if (!currentToolbar) {
                unresolvedControls += 1;
                break;
            }
            const currentTriggers = Array.from(
                currentToolbar.querySelectorAll('button[aria-haspopup]')
            ).filter(rendered).filter(button => !button.disabled);
            const trigger = currentTriggers[index];
            if (!trigger || !safeFilterClick(trigger)) continue;
            triggersChecked += 1;
            await settle();

            const scope = trigger.closest('.dropdown') || currentToolbar;
            const scopedButtons = Array.from(scope.querySelectorAll('button'));
            const clearControl = scopedButtons.find(button =>
                rendered(button) && clearLabels.has(normalizedLabel(button))
            ) || null;
            if (!clearControl) {
                if (trigger.getAttribute('aria-expanded') === 'true') {
                    safeFilterClick(trigger);
                    await settle();
                }
                continue;
            }
            filterControlsFound += 1;
            if (clearControl.disabled ||
                clearControl.getAttribute('aria-disabled') === 'true') {
                alreadyClear += 1;
            } else if (safeFilterClick(clearControl)) {
                clearActions += 1;
                await settle();
            } else {
                unresolvedControls += 1;
            }

            const commitControl = Array.from(scope.querySelectorAll('button'))
                .find(button => rendered(button) &&
                    commitLabels.has(normalizedLabel(button)) &&
                    !button.disabled) || null;
            if (commitControl && safeFilterClick(commitControl)) {
                commitActions += 1;
                await settle();
            } else if (trigger.isConnected &&
                trigger.getAttribute('aria-expanded') === 'true') {
                safeFilterClick(trigger);
                await settle();
            }
        }
        let afterCount = document.querySelectorAll(rowSelector).length;
        let previousCount = -1;
        let stableSamples = 0;
        for (let sample = 0; sample < 20; sample += 1) {
            await settle();
            afterCount = document.querySelectorAll(rowSelector).length;
            if (afterCount === previousCount) {
                stableSamples += 1;
            } else {
                stableSamples = 0;
                previousCount = afterCount;
            }
            if (sample >= 4 && stableSamples >= 3) break;
        }
        const verified = filterControlsFound > 0 &&
            unresolvedControls === 0 &&
            filterControlsFound === clearActions + alreadyClear &&
            afterCount >= beforeCount;
        return {
            status: verified ? 'full_scope_verified' : 'full_scope_unverified',
            verified_full_scope: verified,
            toolbar_found: true,
            dropdown_triggers_checked: triggersChecked,
            filter_controls_found: filterControlsFound,
            filter_clear_actions: clearActions,
            filter_controls_already_clear: alreadyClear,
            commit_actions: commitActions,
            unresolved_filter_controls: unresolvedControls,
            conversation_rows_before: beforeCount,
            conversation_rows_after: afterCount,
            stable_row_count_samples: stableSamples,
            conversation_buttons_clicked: 0
        };
    }
"""


def _safe_frame_location(frame) -> str:
    """Return a query-free frame URL/path suitable for safe metrics."""
    raw_url = getattr(frame, "url", "")
    if callable(raw_url):
        try:
            raw_url = raw_url()
        except Exception:
            return "unknown"
    if not isinstance(raw_url, str) or not raw_url:
        return "unknown"
    try:
        parsed = urlsplit(raw_url)
    except (TypeError, ValueError):
        return "unknown"
    if parsed.scheme in {"http", "https"}:
        hostname = parsed.hostname or "unknown"
        try:
            parsed_port = parsed.port
        except ValueError:
            parsed_port = None
        port = f":{parsed_port}" if parsed_port else ""
        return f"{parsed.scheme}://{hostname}{port}{parsed.path or '/'}"
    if parsed.scheme:
        return f"{parsed.scheme}:{parsed.path}"
    return parsed.path or "unknown"


def _discover_inbox_frames(page) -> list:
    """Use frames from the existing shared page; never launch a browser."""
    try:
        frames = list(page.frames)
    except (AttributeError, TypeError):
        frames = []
    if not frames:
        frames = [page]
    unique_frames = []
    seen = set()
    for frame in frames:
        frame_key = id(frame)
        if frame_key in seen:
            continue
        seen.add(frame_key)
        # Read the name for frame discovery, but never include it in logs.
        frame_name = getattr(frame, "name", "")
        if callable(frame_name):
            try:
                frame_name()
            except Exception:
                pass
        unique_frames.append(frame)
    return unique_frames


def _frame_selector_diagnostics(frame) -> dict:
    keys = (
        "candidate_rows", "href_inbox", "data_href_inbox",
        "data_url_inbox", "conversation_id", "thread_id",
        "conversation_class", "message_row_class", "semantic_rows",
        "appfolio_list_rows", "iframe_elements",
    )
    try:
        raw = frame.evaluate(_INBOX_FRAME_DIAGNOSTICS_SCRIPT) or {}
    except Exception:
        raw = {}
        evaluation_errors = 1
    else:
        evaluation_errors = 0
    if not isinstance(raw, dict):
        raw = {}
        evaluation_errors = 1
    result = {
        key: int(raw.get(key, 0) or 0)
        for key in keys
    }
    result["evaluation_errors"] = evaluation_errors
    return result


def diagnose_appfolio_inbox_structure(
    page, *, attempt: str = "manual", frames=None
) -> list[dict]:
    """Read-only frame/selector inventory with no message text or contents."""
    frames = frames if frames is not None else _discover_inbox_frames(page)
    print(
        f"[AF-InboxFrameMetrics] attempt={attempt} frame_count={len(frames)}"
    )
    diagnostics = []
    for index, frame in enumerate(frames):
        counts = _frame_selector_diagnostics(frame)
        location = _safe_frame_location(frame)
        diagnostics.append(
            {"frame_index": index, "location": location, **counts}
        )
        count_text = " ".join(
            f"{key}={value}" for key, value in counts.items()
        )
        print(
            f"[AF-InboxFrameMetrics] attempt={attempt} frame={index} "
            f"location={location} {count_text}"
        )
    return diagnostics


_UNSAFE_DOM_DIAGNOSTIC_KEYS = {
    "text",
    "inner_text",
    "innertext",
    "text_content",
    "textcontent",
    "content",
    "message",
    "message_text",
    "preview",
    "contact",
    "contact_name",
    "sender",
    "sender_name",
    "property",
    "property_address",
    "unit",
    "assignee",
    "assigned_user",
    "attribute_value",
    "value",
}


def _validate_sanitized_dom_diagnostic(value) -> None:
    """Fail closed if a diagnostic result contains a raw-content field."""
    if isinstance(value, dict):
        for key, nested in value.items():
            if str(key).lower() in _UNSAFE_DOM_DIAGNOSTIC_KEYS:
                raise ValueError(
                    f"unsafe DOM diagnostic field returned: {key}"
                )
            _validate_sanitized_dom_diagnostic(nested)
    elif isinstance(value, list):
        for nested in value:
            _validate_sanitized_dom_diagnostic(nested)


def diagnose_appfolio_inbox_row_structure(page) -> dict:
    """
    Inventory repeated inbox row/card structures without opening a thread.

    DOM text is represented only by positional placeholders and character
    counts. Raw IDs, data values, accessible labels, and navigation targets
    are represented only by per-report tokens and route shapes.
    """
    frames = _discover_inbox_frames(page)
    report = {
        "schema_version": 3,
        "sanitized": True,
        "frame_count": len(frames),
        "frames": [],
    }
    for index, frame in enumerate(frames):
        try:
            structure = frame.evaluate(
                _INBOX_ROW_STRUCTURE_DIAGNOSTIC_SCRIPT
            ) or {}
            if not isinstance(structure, dict):
                raise TypeError("diagnostic result was not an object")
            _validate_sanitized_dom_diagnostic(structure)
            frame_report = {
                "frame_index": index,
                "location": _safe_frame_location(frame),
                "evaluation_status": "ok",
                "structure": structure,
            }
        except Exception as error:
            frame_report = {
                "frame_index": index,
                "location": _safe_frame_location(frame),
                "evaluation_status": "failed",
                "error_type": type(error).__name__,
                "structure": {},
            }
        report["frames"].append(frame_report)
    _validate_sanitized_dom_diagnostic(report)
    return report


def ensure_appfolio_inbox_full_monitoring_scope(page) -> dict:
    """Clear persisted list filters without touching conversation rows."""
    try:
        result = page.evaluate(_INBOX_FILTER_SCOPE_RESET_SCRIPT) or {}
    except Exception as error:
        raise RuntimeError(
            "AppFolio inbox filter-scope verification failed"
        ) from error
    if not isinstance(result, dict):
        raise RuntimeError(
            "AppFolio inbox filter-scope verification returned invalid data"
        )
    print(
        "[AF-InboxFilterScope] "
        f"status={result.get('status', 'unknown')} "
        f"triggers={int(result.get('dropdown_triggers_checked', 0) or 0)} "
        f"filter_controls={int(result.get('filter_controls_found', 0) or 0)} "
        f"cleared={int(result.get('filter_clear_actions', 0) or 0)} "
        f"already_clear={int(result.get('filter_controls_already_clear', 0) or 0)} "
        f"rows_before={int(result.get('conversation_rows_before', 0) or 0)} "
        f"rows_after={int(result.get('conversation_rows_after', 0) or 0)} "
        "conversation_clicks=0"
    )
    if not result.get("verified_full_scope"):
        raise RuntimeError(
            "AppFolio inbox full-monitoring filter scope could not be verified"
        )
    if int(result.get("conversation_buttons_clicked", 0) or 0) != 0:
        raise RuntimeError(
            "AppFolio inbox filter reset violated the conversation-click guard"
        )
    return result


def _extract_rows_from_frame(frame) -> list[dict]:
    try:
        rows = frame.evaluate(_INBOX_LIGHTWEIGHT_ROWS_SCRIPT) or []
    except Exception:
        return []
    return [dict(row) for row in rows if isinstance(row, dict)]


def _normalize_lightweight_rows(raw_rows: list[dict]) -> list[dict]:
    rows = []
    seen_events = set()
    for raw_row in raw_rows:
        row = dict(raw_row or {})
        raw_timestamp = str(row.get("timestamp", "") or "").strip()
        relative_timestamp = re.fullmatch(
            r"(?:just\s+now|yesterday|\d+\s*(?:s|sec|secs|second|seconds|"
            r"m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days|"
            r"w|wk|wks|week|weeks|mo|mos|month|months|y|yr|yrs|year|years)"
            r"(?:\s+ago)?)",
            raw_timestamp,
            re.I,
        )
        row["timestamp"] = "" if relative_timestamp else raw_timestamp
        thread_id = appfolio_thread_id_from_href(row.get("href", ""))
        if not thread_id:
            thread_basis = "|".join(
                str(row.get(key, "") or "").strip().lower()
                for key in (
                    "contact_name", "contact_role", "list_detail",
                    "property", "unit",
                )
            )
            if not thread_basis.strip("|"):
                continue
            thread_id = "list-" + hashlib.sha256(
                thread_basis.encode("utf-8")
            ).hexdigest()
        if not row.get("source_message_id"):
            event_basis = "|".join(
                str(value or "").strip().lower()
                for value in (
                    thread_id,
                    row.get("timestamp"),
                    row.get("preview"),
                )
            )
            row["source_message_id"] = "list-event-" + hashlib.sha256(
                event_basis.encode("utf-8")
            ).hexdigest()
        event_identity = (thread_id, row["source_message_id"])
        if event_identity in seen_events:
            continue
        seen_events.add(event_identity)
        row["thread_id"] = thread_id
        row["selector_key"] = inbox_selector_key(row)
        row["event_key"] = inbox_event_key(row)
        row["conversation_identity_key"] = conversation_identity_key(row)
        row["conversation_state_key"] = conversation_state_key(row)
        rows.append(row)
    return rows


def _read_lightweight_inbox_rows(page) -> list[dict]:
    """Read list metadata from every existing frame without opening threads."""
    frames = _discover_inbox_frames(page)
    diagnose_appfolio_inbox_structure(
        page, attempt="initial", frames=frames
    )
    raw_rows = []
    for frame in frames:
        raw_rows.extend(_extract_rows_from_frame(frame))
    rows = _normalize_lightweight_rows(raw_rows)
    if rows:
        return rows

    # One bounded retry handles frames and rows attached after DOMContentLoaded.
    try:
        page.wait_for_timeout(1500)
    except Exception:
        pass
    frames = _discover_inbox_frames(page)
    diagnose_appfolio_inbox_structure(
        page, attempt="dynamic_retry", frames=frames
    )
    raw_rows = []
    for frame in frames:
        raw_rows.extend(_extract_rows_from_frame(frame))
    rows = _normalize_lightweight_rows(raw_rows)
    if not rows:
        print(
            f"[AF-InboxMetrics] extraction_empty=1 frame_count={len(frames)}"
        )
    return rows


_INBOX_SCROLL_PROGRESS_SCRIPT = r"""
    () => {
        // AF_INBOX_SCROLL_PROGRESS_READ_ONLY
        // One bounded step toward revealing more list rows. Never opens a
        // conversation: the load-more/pagination control this looks for is
        // explicitly required to sit outside the row list itself, and the
        // scroll fallback only ever adjusts scrollTop on a container.
        const listSelector = 'ul.list-group';
        const list = document.querySelector(listSelector);
        const isVisible = element => {
            if (!(element instanceof Element)) return false;
            const rect = element.getBoundingClientRect();
            const style = window.getComputedStyle(element);
            return rect.width > 0 && rect.height > 0 &&
                style.visibility !== 'hidden' && style.display !== 'none';
        };
        const text = element =>
            element
                ? (element.innerText || element.textContent || '').trim()
                : '';

        const loadMoreCandidates = Array.from(
            document.querySelectorAll('button, a[role="button"], a')
        ).filter(element =>
            !element.closest(listSelector) &&
            isVisible(element) &&
            /\b(load more|show more|more conversations|next page|next)\b/i
                .test(text(element))
        );
        const loadMore = loadMoreCandidates[0] || null;

        if (loadMore) {
            const disabled = Boolean(
                loadMore.disabled ||
                loadMore.getAttribute('aria-disabled') === 'true' ||
                loadMore.classList.contains('disabled')
            );
            if (disabled) {
                return {
                    action: 'load_more_disabled',
                    load_more_present: true,
                    load_more_disabled: true,
                    load_more_clicked: false,
                    scrolled: false,
                };
            }
            loadMore.click();
            return {
                action: 'load_more_clicked',
                load_more_present: true,
                load_more_disabled: false,
                load_more_clicked: true,
                scrolled: false,
            };
        }

        let container = list;
        while (container && container !== document.body) {
            const style = window.getComputedStyle(container);
            const scrollable = (
                container.scrollHeight > container.clientHeight + 2 &&
                /(?:auto|scroll)/.test(style.overflowY)
            );
            if (scrollable) break;
            container = container.parentElement;
        }
        if (!container || container === document.body) {
            return {
                action: 'no_scroll_target',
                load_more_present: false,
                load_more_disabled: false,
                load_more_clicked: false,
                scrolled: false,
            };
        }
        const scrollTopBefore = container.scrollTop;
        const scrollHeightBefore = container.scrollHeight;
        container.scrollTop = container.scrollHeight;
        return {
            action: 'scrolled',
            load_more_present: false,
            load_more_disabled: false,
            load_more_clicked: false,
            scrolled: true,
            scroll_top_before: scrollTopBefore,
            scroll_top_after: container.scrollTop,
            scroll_height_before: scrollHeightBefore,
            scroll_height_after: container.scrollHeight,
        };
    }
"""


def _attempt_inbox_scroll_progress(page) -> dict:
    """One bounded scroll/load-more step. Never clicks a conversation row."""
    frames = _discover_inbox_frames(page)
    for frame in frames:
        try:
            result = frame.evaluate(_INBOX_SCROLL_PROGRESS_SCRIPT)
        except Exception:
            continue
        if isinstance(result, dict) and result.get("action") not in (
            None, "no_scroll_target",
        ):
            return result
    return {
        "action": "no_scroll_target",
        "load_more_present": False,
        "load_more_disabled": False,
        "load_more_clicked": False,
        "scrolled": False,
    }


def resolve_target_row_via_full_inbox_traversal(
    page,
    target_selector_key: str,
    *,
    read_rows_fn=None,
    scroll_step_fn=None,
    max_iterations: int = None,
    max_seconds: float = None,
    stable_iterations_required: int = None,
    wait_between_ms: int = None,
    clock=None,
) -> dict:
    """
    Locate one exact conversation row by its selector-key fingerprint.

    Starts from whatever the inbox already has rendered. Only if the target
    is absent from that set does it traverse further, via safe scroll/
    load-more steps only - never by opening a conversation, changing read
    state/assignment/filters, or writing anything. Intended for targeted
    lookups (controlled QA, explicit management lookup, recovery/backfill,
    ownership resolution) where the target may not be in the initial
    render - NOT for normal lightweight inbox polling, which must stay a
    fast, low-bandwidth pass over whatever AppFolio already rendered.
    """
    read_rows = read_rows_fn or (lambda p: _read_lightweight_inbox_rows(p))
    scroll_step = scroll_step_fn or (
        lambda p: _attempt_inbox_scroll_progress(p)
    )
    clock_fn = clock or time.monotonic
    max_iterations = (
        max_iterations
        if max_iterations is not None
        else int(os.getenv("RACHEL_INBOX_TRAVERSAL_MAX_ITERATIONS", "40"))
    )
    max_seconds = (
        max_seconds
        if max_seconds is not None
        else float(os.getenv("RACHEL_INBOX_TRAVERSAL_MAX_SECONDS", "90"))
    )
    stable_iterations_required = (
        stable_iterations_required
        if stable_iterations_required is not None
        else int(os.getenv("RACHEL_INBOX_TRAVERSAL_STABLE_ROUNDS", "3"))
    )
    wait_between_ms = (
        wait_between_ms
        if wait_between_ms is not None
        else int(os.getenv("RACHEL_INBOX_TRAVERSAL_WAIT_MS", "400"))
    )
    expected = str(target_selector_key or "").strip().lower()

    result = {
        "initial_rows": 0,
        "unique_rows_seen": 0,
        "iterations": 0,
        "target_found": False,
        "target_match_count": 0,
        "end_condition": "",
        "conversation_opens": 0,
        "writes": 0,
        "matched_row": None,
    }

    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        result["end_condition"] = "invalid_target_selector_key"
        return result

    # Keyed by (thread_id, source_message_id) - a specific row occurrence,
    # not by selector_key. Two distinct conversations can legitimately
    # collide on selector_key content; collapsing them here would hide a
    # genuine ambiguous-target condition instead of reporting it.
    unique_rows: dict = {}

    def _absorb(rows) -> int:
        added = 0
        for raw_row in rows or []:
            row = dict(raw_row)
            row["selector_key"] = inbox_selector_key(row)
            identity = (
                str(row.get("thread_id", "") or ""),
                str(row.get("source_message_id", "") or ""),
            )
            if identity in unique_rows:
                continue
            unique_rows[identity] = row
            added += 1
        return added

    def _matches() -> list:
        return [
            r for r in unique_rows.values() if r.get("selector_key") == expected
        ]

    def _settle(matches) -> bool:
        """Record a found/ambiguous outcome if one exists. Returns True if done."""
        if len(matches) == 1:
            result.update(
                target_found=True,
                target_match_count=1,
                end_condition="target_found",
                matched_row=matches[0],
            )
            return True
        if len(matches) > 1:
            result.update(
                target_found=False,
                target_match_count=len(matches),
                end_condition="target_ambiguous",
            )
            return True
        return False

    initial_rows = read_rows(page)
    result["initial_rows"] = len(initial_rows or [])
    _absorb(initial_rows)
    result["unique_rows_seen"] = len(unique_rows)
    if _settle(_matches()):
        return result

    started = clock_fn()
    stable_rounds = 0

    while True:
        if result["iterations"] >= max_iterations:
            result["end_condition"] = "max_iterations_reached"
            break
        if (clock_fn() - started) >= max_seconds:
            result["end_condition"] = "max_seconds_reached"
            break

        step = scroll_step(page) or {}
        result["iterations"] += 1

        if step.get("load_more_present") and step.get("load_more_disabled"):
            result["end_condition"] = "load_more_disabled"
            break
        if not (step.get("load_more_clicked") or step.get("scrolled")):
            result["end_condition"] = "list_end_no_scroll_target"
            break

        try:
            page.wait_for_timeout(wait_between_ms)
        except Exception:
            pass

        added = _absorb(read_rows(page))
        result["unique_rows_seen"] = len(unique_rows)
        if _settle(_matches()):
            return result

        if added > 0:
            stable_rounds = 0
        else:
            stable_rounds += 1
            if stable_rounds >= stable_iterations_required:
                result["end_condition"] = "stable_no_new_rows"
                break

    result["target_found"] = False
    if not result["end_condition"]:
        result["end_condition"] = "max_iterations_reached"
    return result


_INBOX_EXACT_ROW_MATCH_SCRIPT = r"""
    target => {
        // AF_INBOX_EXACT_ROW_MATCH_READ_ONLY
        const clean = value => String(value || '').trim().replace(/\s+/g, ' ');
        const directChild = (element, selector) =>
            Array.from(element.children || []).find(child =>
                child.matches(selector)
            ) || null;
        const cleanText = element => clean(
            element ? (element.innerText || element.textContent || '') : ''
        );
        const rows = [];
        for (const button of document.querySelectorAll('ul.list-group > button')) {
            const row = directChild(button, 'li.list-group-item');
            if (!row) continue;
            const layout = directChild(row, 'div.d-flex');
            const content = layout
                ? directChild(layout, 'div.d-flex.flex-column') : null;
            if (!content) continue;
            const heading = directChild(
                content, 'div.d-flex.justify-content-between'
            );
            const contact = cleanText(heading
                ? heading.querySelector('span[data-testid], span.text-truncate')
                : null);
            const detail = cleanText(directChild(content, 'div.text-truncate'));
            const preview = cleanText(directChild(content, 'span.text-truncate'));
            const assigned = cleanText(content.querySelector([
                '[data-testid*="assignee"]', '[data-testid*="assigned"]',
                '[class*="assignee"]', '[class*="assigned-user"]'
            ].join(',')));
            const roleMatch = cleanText(row).match(
                /\b(TENANT|VENDOR|OWNER|RESIDENT)\b/i
            );
            const candidate = {
                contact_name: contact,
                contact_role: roleMatch ? roleMatch[1].toLowerCase() : '',
                list_detail: detail,
                preview: preview,
                assigned_user: assigned
            };
            const matches = ['contact_name', 'contact_role', 'list_detail', 'preview']
                .every(key => clean(candidate[key]).toLowerCase() ===
                    clean(target[key]).toLowerCase()) &&
                (!clean(target.assigned_user) ||
                    clean(candidate.assigned_user).toLowerCase() ===
                    clean(target.assigned_user).toLowerCase());
            if (matches) rows.push(button);
        }
        return {match_count: rows.length};
    }
"""


_INBOX_EXACT_ROW_CLICK_SCRIPT = r"""
    target => {
        // AF_INBOX_EXACT_ROW_CLICK
        const clean = value => String(value || '').trim().replace(/\s+/g, ' ');
        const directChild = (element, selector) =>
            Array.from(element.children || []).find(child =>
                child.matches(selector)
            ) || null;
        const cleanText = element => clean(
            element ? (element.innerText || element.textContent || '') : ''
        );
        const matches = [];
        for (const button of document.querySelectorAll('ul.list-group > button')) {
            const row = directChild(button, 'li.list-group-item');
            if (!row || !button.closest('ul.list-group')) continue;
            const layout = directChild(row, 'div.d-flex');
            const content = layout
                ? directChild(layout, 'div.d-flex.flex-column') : null;
            if (!content) continue;
            const heading = directChild(
                content, 'div.d-flex.justify-content-between'
            );
            const contact = cleanText(heading
                ? heading.querySelector('span[data-testid], span.text-truncate')
                : null);
            const detail = cleanText(directChild(content, 'div.text-truncate'));
            const preview = cleanText(directChild(content, 'span.text-truncate'));
            const assigned = cleanText(content.querySelector([
                '[data-testid*="assignee"]', '[data-testid*="assigned"]',
                '[class*="assignee"]', '[class*="assigned-user"]'
            ].join(',')));
            const roleMatch = cleanText(row).match(
                /\b(TENANT|VENDOR|OWNER|RESIDENT)\b/i
            );
            const candidate = {
                contact_name: contact,
                contact_role: roleMatch ? roleMatch[1].toLowerCase() : '',
                list_detail: detail,
                preview: preview,
                assigned_user: assigned
            };
            const exact = ['contact_name', 'contact_role', 'list_detail', 'preview']
                .every(key => clean(candidate[key]).toLowerCase() ===
                    clean(target[key]).toLowerCase()) &&
                (!clean(target.assigned_user) ||
                    clean(candidate.assigned_user).toLowerCase() ===
                    clean(target.assigned_user).toLowerCase());
            if (exact) matches.push(button);
        }
        if (matches.length !== 1) {
            return {clicked: false, match_count: matches.length};
        }
        matches[0].click();
        return {clicked: true, match_count: 1};
    }
"""


_INBOX_THREAD_READ_SCRIPT = r"""
    () => {
        // AF_INBOX_THREAD_READ_ONLY
        const clean = value => String(value || '').trim().replace(/\s+/g, ' ');
        const text = element => clean(
            element ? (element.innerText || element.textContent || '') : ''
        );
        const visible = element => {
            if (!(element instanceof Element)) return false;
            const rect = element.getBoundingClientRect();
            const style = window.getComputedStyle(element);
            return rect.width > 0 && rect.height > 0 &&
                style.display !== 'none' && style.visibility !== 'hidden';
        };
        const exactText = (element, expected) =>
            text(element).toLowerCase() === expected.toLowerCase();
        const backControls = Array.from(
            document.querySelectorAll('button, a')
        ).filter(visible).filter(element => exactText(element, 'Back to Inbox'));
        const hasClasses = (element, expected) => expected.every(name =>
            element.classList.contains(name)
        );
        const directChildren = (element, predicate) => element
            ? Array.from(element.children || []).filter(predicate) : [];
        const leafExactText = (root, expected) => Array.from(
            root ? root.querySelectorAll('*') : []
        ).filter(visible).filter(element => exactText(element, expected))
            .filter(element => !Array.from(element.children || [])
                .some(child => exactText(child, expected)));
        const roots = Array.from(document.querySelectorAll('section'))
            .filter(visible)
            .map(root => {
                const historyCandidates = directChildren(root, child =>
                    child.tagName === 'MAIN' &&
                    child.getAttribute('role') === 'list'
                );
                const composerCandidates = directChildren(root, child =>
                    child.tagName === 'DIV' &&
                    Array.from(child.querySelectorAll('textarea'))
                        .filter(visible).length === 1 &&
                    Array.from(child.querySelectorAll('button'))
                        .filter(visible)
                        .filter(button => exactText(button, 'Send')).length === 1 &&
                    leafExactText(child, 'Send via SMS').length === 1
                );
                const historyIndex = historyCandidates.length === 1
                    ? Array.from(root.children).indexOf(historyCandidates[0]) : -1;
                const composerIndex = composerCandidates.length === 1
                    ? Array.from(root.children).indexOf(composerCandidates[0]) : -1;
                const headerCandidates = directChildren(root, child => {
                    const childIndex = Array.from(root.children).indexOf(child);
                    const roles = Array.from(child.querySelectorAll('span'))
                        .filter(visible).filter(element =>
                            /^(tenant|vendor|owner|resident)$/i.test(text(element))
                        );
                    return child.tagName === 'DIV' && roles.length === 1 &&
                        childIndex >= 0 && childIndex < historyIndex;
                });
                if (
                    headerCandidates.length !== 1 ||
                    historyCandidates.length !== 1 ||
                    composerCandidates.length !== 1 ||
                    historyIndex < 0 || composerIndex <= historyIndex
                ) return null;
                return {
                    root: root,
                    header: headerCandidates[0],
                    history: historyCandidates[0],
                    composer: composerCandidates[0]
                };
            }).filter(Boolean);
        const structure = roots.length === 1 ? roots[0] : null;
        const header = structure ? structure.header : null;
        const history = structure ? structure.history : null;
        const composer = structure ? structure.composer : null;
        const roleElements = header
            ? Array.from(header.querySelectorAll('span')).filter(visible)
                .filter(element =>
                    /^(tenant|vendor|owner|resident)$/i.test(text(element))
                )
            : [];
        const identityContainer = roleElements.length === 1
            ? roleElements[0].parentElement : null;
        const contactLinks = identityContainer
            ? Array.from(identityContainer.querySelectorAll('a'))
                .filter(visible)
                .filter(element => !String(element.getAttribute('href') || '')
                    .toLowerCase().startsWith('tel:'))
            : [];
        const phoneElements = header
            ? Array.from(header.querySelectorAll('a[href^="tel:"]')).filter(visible)
            : [];
        const historyWrappers = history
            ? directChildren(history, child => child.tagName === 'DIV') : [];
        const messageItems = history
            ? Array.from(history.querySelectorAll('[role="listitem"]'))
                .filter(visible)
                .filter(item => !item.parentElement.closest('[role="listitem"]'))
            : [];
        const nonMessageHistoryNodes = history
            ? Array.from(history.querySelectorAll('[role="listitem"]'))
                .filter(visible).length - messageItems.length
            : 0;
        let invalidMessageStructures = 0;
        const messageFailureCounts = {};
        const failMessage = reason => {
            invalidMessageStructures += 1;
            messageFailureCounts[reason] =
                (messageFailureCounts[reason] || 0) + 1;
            return null;
        };
        const messages = messageItems.map((item, index) => {
            const inbound = item.classList.contains('text-start');
            const outbound = item.classList.contains('text-end');
            if (inbound === outbound) {
                return failMessage('direction_ambiguous');
            }
            const isTextBubble = child => child.tagName === 'DIV' &&
                hasClasses(child, [
                    'd-inline-block', 'text-start', 'rounded-3',
                    'py-2', 'px-3'
                ]);
            const alignmentRows = directChildren(item, child =>
                child.tagName === 'DIV' &&
                child.classList.contains('d-flex') &&
                child.classList.contains('align-items-center') &&
                (inbound
                    ? !child.classList.contains('justify-content-end')
                    : child.classList.contains('justify-content-end')) &&
                directChildren(child, isTextBubble).length === 1
            );
            const alignedBubbleCandidates = alignmentRows.length === 1
                ? directChildren(alignmentRows[0], isTextBubble) : [];
            // The captured AppFolio history contains a second deterministic
            // outbound structure for attachment/status cards. Those cards are
            // direct list-item children rather than children of the alignment
            // row used by ordinary text bubbles.
            const directCardCandidates = outbound
                ? directChildren(
                    item,
                    child => child.tagName === 'DIV' &&
                        hasClasses(child, [
                            'd-inline-block', 'text-start', 'rounded-3',
                            'py-2', 'px-3', 'border-0', 'text-muted'
                        ])
                ) : [];
            if (alignmentRows.length > 1) {
                return failMessage('body_alignment_region_ambiguous');
            }
            const bodyContainerCount =
                alignedBubbleCandidates.length + directCardCandidates.length;
            if (bodyContainerCount !== 1) {
                return failMessage('body_container_ambiguous');
            }
            const bubble = alignedBubbleCandidates[0] ||
                directCardCandidates[0];
            /* Preserve the ordinary alignment-row assertion when this is not
               the captured direct attachment/status-card variant. */
            if (!directCardCandidates.length && alignmentRows.length !== 1) {
                return failMessage('body_alignment_region_ambiguous');
            }
            const bodyText = text(bubble).substring(0, 10000);
            if (!bodyText) {
                return failMessage('body_empty');
            }
            const metadataRows = directChildren(item, child =>
                child !== alignmentRows[0] &&
                child !== bubble &&
                child.tagName === 'DIV' &&
                child.querySelectorAll('small').length >= 1
            );
            const metadataParts = metadataRows.length
                ? Array.from(metadataRows[metadataRows.length - 1]
                    .querySelectorAll('small'))
                    .filter(visible)
                : [];
            const timeElement = metadataParts.length
                ? metadataParts[metadataParts.length - 1] : null;
            const senderElement = metadataParts.length > 1
                ? metadataParts[0] : null;
            return {
                source_message_id: clean(
                    item.getAttribute('data-message-id') ||
                    item.getAttribute('data-id') ||
                    bubble.getAttribute('data-message-id') ||
                    bubble.getAttribute('data-id') || ''
                ),
                direction: inbound ? 'inbound' : 'outbound',
                timestamp: timeElement ? text(timeElement).substring(0, 160) : '',
                message_text: bodyText,
                sender_classification: senderElement
                    ? text(senderElement).substring(0, 160) : '',
                rendered_index: index
            };
        }).filter(Boolean);
        const smsLeafLabels = leafExactText(composer, 'Send via SMS');
        const composerTextareas = composer
            ? Array.from(composer.querySelectorAll('textarea')).filter(visible)
            : [];
        const sendControls = composer
            ? Array.from(composer.querySelectorAll('button')).filter(visible)
                .filter(button => exactText(button, 'Send'))
            : [];
        const messageStructureValid = Boolean(
            history && messageItems.length > 0 &&
            messages.length === messageItems.length &&
            invalidMessageStructures === 0
        );
        const structureFailureReasons = [];
        if (roots.length !== 1) structureFailureReasons.push(
            roots.length > 1 ? 'ambiguous_thread_roots' : 'thread_root_absent'
        );
        if (!history) structureFailureReasons.push('history_region_absent');
        if (!composer) structureFailureReasons.push('composer_region_absent');
        if (backControls.length !== 1) structureFailureReasons.push(
            backControls.length > 1 ? 'ambiguous_back_controls' : 'back_control_absent'
        );
        if (composerTextareas.length !== 1) structureFailureReasons.push(
            'composer_textarea_count_invalid'
        );
        if (sendControls.length !== 1) structureFailureReasons.push(
            'send_control_count_invalid'
        );
        if (smsLeafLabels.length !== 1) structureFailureReasons.push(
            'send_via_sms_label_count_invalid'
        );
        if (contactLinks.length !== 1) structureFailureReasons.push(
            'contact_candidate_count_invalid'
        );
        if (roleElements.length !== 1) structureFailureReasons.push(
            'role_candidate_count_invalid'
        );
        const scrollable = Boolean(
            history && history.scrollHeight > history.clientHeight + 2
        );
        const virtualizationHintsPresent = Boolean(history &&
            history.querySelector([
                '[style*="transform: translate"]', '[data-virtualized]',
                '[class*="virtual"]', '[class*="viewport"]',
                '[class*="sentinel"]'
            ].join(','))
        );
        const lazyLoadHintsPresent = Boolean(history &&
            history.querySelector([
                '[aria-busy="true"]', '[loading="lazy"]',
                '[class*="lazy"]', '[class*="sentinel"]'
            ].join(','))
        );
        return {
            back_control_count: backControls.length,
            thread_root_count: roots.length,
            header_region_count: structure ? 1 : 0,
            history_region_count: history ? 1 : 0,
            composer_region_count: composer ? 1 : 0,
            composer_present: Boolean(composer),
            composer_textarea_count: composerTextareas.length,
            composer_value_length: composerTextareas.length === 1
                ? String(composerTextareas[0].value || '').length : -1,
            send_control_count: sendControls.length,
            send_control_disabled: sendControls.length === 1
                ? Boolean(sendControls[0].disabled ||
                    sendControls[0].getAttribute('aria-disabled') === 'true')
                : true,
            send_via_sms_label_count: smsLeafLabels.length,
            send_via_sms_present: smsLeafLabels.length === 1,
            thread_history_present: Boolean(history),
            history_wrapper_count: historyWrappers.length,
            non_message_history_node_count: Math.max(0, nonMessageHistoryNodes),
            rendered_message_item_count: messageItems.length,
            invalid_message_structure_count: invalidMessageStructures,
            message_failure_counts: messageFailureCounts,
            structure_failure_reasons: structureFailureReasons,
            message_structure_valid: messageStructureValid,
            message_count: messages.length,
            messages: messages,
            contact_name: contactLinks.length === 1
                ? text(contactLinks[0]).substring(0, 240) : '',
            contact_candidate_count: contactLinks.length,
            contact_role: roleElements.length === 1
                ? text(roleElements[0]).toLowerCase() : '',
            role_candidate_count: roleElements.length,
            headings: contactLinks.length === 1
                ? [text(contactLinks[0]).substring(0, 240)] : [],
            context_fields: header ? [text(header).substring(0, 2000)] : [],
            detail_context: header ? text(header).substring(0, 2000) : '',
            phone: phoneElements.length === 1
                ? text(phoneElements[0]).substring(0, 80) : '',
            history_scrollable: scrollable,
            history_at_scroll_start: Boolean(history && history.scrollTop <= 1),
            history_at_scroll_end: Boolean(history &&
                history.scrollTop + history.clientHeight >= history.scrollHeight - 2),
            history_client_height: history ? history.clientHeight : 0,
            history_scroll_height: history ? history.scrollHeight : 0,
            virtualization_hints_present: virtualizationHintsPresent,
            lazy_load_hints_present: lazyLoadHintsPresent
        };
    }
"""


_INBOX_THREAD_READY_SCRIPT = r"""
    () => {
        // AF_INBOX_THREAD_STRUCTURE_READY_ONLY
        // Structure-only readiness probe. It returns no conversation text.
        const clean = value => String(value || '').trim().replace(/\s+/g, ' ');
        const text = element => clean(
            element ? (element.innerText || element.textContent || '') : ''
        );
        const visible = element => {
            if (!(element instanceof Element)) return false;
            const rect = element.getBoundingClientRect();
            const style = window.getComputedStyle(element);
            return rect.width > 0 && rect.height > 0 &&
                style.display !== 'none' && style.visibility !== 'hidden';
        };
        const exactText = (element, expected) =>
            text(element).toLowerCase() === expected.toLowerCase();
        const hasClasses = (element, expected) => expected.every(name =>
            element.classList.contains(name)
        );
        const directChildren = (element, predicate) => element
            ? Array.from(element.children || []).filter(predicate) : [];
        const leafExactText = (root, expected) => Array.from(
            root ? root.querySelectorAll('*') : []
        ).filter(visible).filter(element => exactText(element, expected))
            .filter(element => !Array.from(element.children || [])
                .some(child => exactText(child, expected)));
        const roots = Array.from(document.querySelectorAll('section'))
            .filter(visible)
            .map(root => {
                const histories = directChildren(root, child =>
                    child.tagName === 'MAIN' &&
                    child.getAttribute('role') === 'list'
                );
                const composers = directChildren(root, child =>
                    child.tagName === 'DIV' &&
                    Array.from(child.querySelectorAll('textarea'))
                        .filter(visible).length === 1 &&
                    Array.from(child.querySelectorAll('button'))
                        .filter(visible)
                        .filter(button => exactText(button, 'Send')).length === 1 &&
                    leafExactText(child, 'Send via SMS').length === 1
                );
                const historyIndex = histories.length === 1
                    ? Array.from(root.children).indexOf(histories[0]) : -1;
                const composerIndex = composers.length === 1
                    ? Array.from(root.children).indexOf(composers[0]) : -1;
                const headers = directChildren(root, child => {
                    const childIndex = Array.from(root.children).indexOf(child);
                    const roles = Array.from(child.querySelectorAll('span'))
                        .filter(visible).filter(element =>
                            /^(tenant|vendor|owner|resident)$/i.test(text(element))
                        );
                    return child.tagName === 'DIV' && roles.length === 1 &&
                        childIndex >= 0 && childIndex < historyIndex;
                });
                if (
                    headers.length !== 1 || histories.length !== 1 ||
                    composers.length !== 1 || historyIndex < 0 ||
                    composerIndex <= historyIndex
                ) return null;
                const messageItems = Array.from(
                    histories[0].querySelectorAll('[role="listitem"]')
                ).filter(visible).filter(item =>
                    !item.parentElement.closest('[role="listitem"]')
                );
                if (messageItems.length < 1) return null;
                return {message_item_count: messageItems.length};
            }).filter(Boolean);
        return {
            ready_thread_root_count: roots.length,
            thread_ready: roots.length === 1,
            rendered_message_item_count: roots.length === 1
                ? roots[0].message_item_count : 0
        };
    }
"""


_INBOX_OPENED_THREAD_DOM_DIAGNOSTIC_SCRIPT = r"""
    () => {
        // SANITIZED_OPENED_THREAD_LIGHT_DOM_DIAGNOSTIC
        // Static structural inventory only: no clicks, scrolling, form input,
        // raw text, raw attribute values, or raw identifiers are returned.
        const round = value => Math.round(Number(value || 0));
        const clean = value => String(value || '').trim().replace(/\s+/g, ' ');
        const renderedText = element => clean(
            element ? (element.innerText || element.textContent || '') : ''
        );
        const visible = element => {
            if (!(element instanceof Element)) return false;
            const rect = element.getBoundingClientRect();
            const style = window.getComputedStyle(element);
            return rect.width > 0 && rect.height > 0 &&
                style.display !== 'none' && style.visibility !== 'hidden' &&
                style.opacity !== '0';
        };
        const all = Array.from(document.querySelectorAll('*'));
        const visibleElements = all.filter(visible);
        const allowedRoles = new Set([
            'alert', 'article', 'button', 'dialog', 'document', 'form',
            'generic', 'group', 'heading', 'img', 'link', 'list',
            'listitem', 'main', 'navigation', 'region', 'separator',
            'status', 'textbox', 'timer', 'toolbar'
        ]);
        const safeClassWords = new Set([
            'active', 'align', 'around', 'avatar', 'back', 'between', 'bg',
            'block', 'body', 'border', 'bottom', 'btn', 'bubble', 'button', 'card',
            'center', 'col', 'column', 'composer', 'contact', 'container',
            'content', 'conversation', 'danger', 'dark', 'detail', 'disabled',
            'd', 'dropdown', 'end', 'flex', 'footer', 'form', 'from', 'gap',
            'group', 'grow', 'h',
            'header', 'history', 'inbox', 'info', 'inline', 'input', 'item',
            'inbound', 'incoming', 'intersection', 'item', 'items', 'justify',
            'lazy', 'left', 'light', 'link', 'list', 'loading', 'm', 'message',
            'muted', 'mx', 'my', 'nav', 'none', 'outbound', 'outgoing',
            'outline', 'overflow', 'p', 'phone', 'position', 'px', 'py',
            'primary', 'property', 'received', 'right', 'rounded', 'row',
            'secondary', 'self', 'sent', 'sentinel', 'shrink', 'sm', 'sms', 'start',
            'stretch', 'success', 'text', 'thread', 'time', 'timestamp',
            'toolbar', 'top', 'truncate', 'unit', 'us', 'viewport', 'virtual',
            'virtualized', 'w', 'warning', 'white', 'window', 'wrap', 'wrapper'
        ]);
        const stableClassToken = token => {
            if (token.length > 80 || !/^[A-Za-z_-][A-Za-z0-9_-]*$/.test(token)) {
                return false;
            }
            if (/@|\+|\d{5,}/.test(token)) return false;
            const parts = token.toLowerCase().split(/[-_]/).filter(Boolean);
            if (!parts.length) return false;
            return parts.every(part => safeClassWords.has(part) || /^\d{1,3}$/.test(part));
        };
        const safeClasses = element => Array.from(element.classList || [])
            .filter(stableClassToken)
            .slice(0, 12);
        const standardAttributeNames = new Set([
            'aria-busy', 'aria-controls', 'aria-describedby', 'aria-expanded',
            'aria-label', 'aria-labelledby', 'aria-live', 'class',
            'contenteditable', 'data-id', 'data-message-id',
            'data-testid', 'data-timestamp', 'datetime', 'dir', 'href', 'id',
            'name', 'placeholder', 'role', 'style', 'tabindex', 'title', 'type'
        ]);
        const safeCustomAttributeName = name => {
            if (!name.startsWith('data-')) return false;
            const parts = name.slice(5).split('-').filter(Boolean);
            return parts.length > 0 && parts.every(part =>
                safeClassWords.has(part) || /^(?:testid|id|timestamp)$/.test(part)
            );
        };
        const attributeNames = element => Array.from(element.attributes || [])
            .map(attribute => String(attribute.name || '').toLowerCase())
            .filter(name => /^[a-z_:][a-z0-9_.:-]*$/.test(name))
            .filter(name => !name.startsWith('on'))
            .filter(name => standardAttributeNames.has(name) ||
                safeCustomAttributeName(name))
            .sort()
            .slice(0, 24);
        const roleFor = element => {
            const role = clean(element.getAttribute('role')).toLowerCase();
            return allowedRoles.has(role) ? role : (role ? 'unrecognized' : '');
        };
        const geometry = element => {
            const rect = element.getBoundingClientRect();
            return {
                x: round(rect.left), y: round(rect.top),
                width: round(rect.width), height: round(rect.height)
            };
        };
        const signature = element => [
            element.tagName.toLowerCase(),
            roleFor(element),
            safeClasses(element).join('.')
        ].join('|');
        const compact = element => ({
            tag_name: element.tagName.toLowerCase(),
            role: roleFor(element),
            stable_class_names: safeClasses(element),
            safe_attribute_names: attributeNames(element),
            visible: visible(element),
            geometry: geometry(element),
            direct_child_count: element.children.length,
            descendant_element_count: element.querySelectorAll('*').length,
            structural_signature: signature(element)
        });
        const hierarchy = element => {
            const nodes = [];
            let current = element;
            for (let depth = 0; current && depth < 7; depth += 1) {
                nodes.unshift({
                    tag_name: current.tagName.toLowerCase(),
                    role: roleFor(current),
                    stable_class_names: safeClasses(current)
                });
                current = current.parentElement;
            }
            return nodes;
        };
        const describe = element => ({
            ...compact(element),
            hierarchy: hierarchy(element)
        });
        const exact = (element, expected) =>
            renderedText(element).toLowerCase() === expected.toLowerCase();
        const controls = visibleElements.filter(element =>
            ['BUTTON', 'A'].includes(element.tagName)
        );
        const backControls = controls.filter(element =>
            exact(element, 'Back to Inbox')
        );
        const smsLabels = visibleElements.filter(element =>
            exact(element, 'Send via SMS')
        );
        const sendControls = controls.filter(element => exact(element, 'Send'));
        const assignmentControls = controls.filter(element =>
            /^(?:assign|assigned)$/i.test(renderedText(element))
        );
        const overflowControls = controls.filter(element => {
            const label = clean(
                element.getAttribute('aria-label') ||
                element.getAttribute('title') || ''
            );
            return /(?:more|overflow|menu|options)/i.test(label) ||
                /(?:overflow|ellipsis|kebab)/i.test(
                    safeClasses(element).join(' ')
                );
        });
        const inputElements = visibleElements.filter(element =>
            element.matches('textarea, input, [contenteditable]')
        );
        const composerCandidates = Array.from(new Set([
            ...smsLabels.map(element => element.closest(
                'form, [role="form"], section, [class*="composer"], [class*="message"]'
            )).filter(Boolean),
            ...inputElements.map(element => element.closest(
                'form, [role="form"], section, [class*="composer"], [class*="message"]'
            )).filter(Boolean)
        ])).filter(visible);
        const legacyHistorySelector = [
            '[data-testid*="message-history"]',
            '[data-testid*="conversation"]',
            '[class*="message-history"]',
            '[class*="conversation-history"]',
            '[class*="thread-history"]', '[class*="messages"]'
        ].join(',');
        const legacyHistoryCandidates = Array.from(
            document.querySelectorAll(legacyHistorySelector)
        ).filter(visible).filter(element =>
            !composerCandidates.includes(element)
        );
        const legacyComposer = smsLabels.map(element => element.closest(
            'form, [role="form"], section, [class*="composer"], [class*="message"]'
        )).find(Boolean) || null;

        const commonAncestor = elements => {
            if (!elements.length) return null;
            let candidate = elements[0];
            while (candidate) {
                if (elements.every(element => candidate.contains(element))) {
                    return candidate;
                }
                candidate = candidate.parentElement;
            }
            return null;
        };
        const rootInputs = [
            backControls[0] || null,
            smsLabels[0] || null,
            inputElements[0] || null
        ].filter(Boolean);
        const structuralRoot = commonAncestor(rootInputs);
        const headerElements = visibleElements.filter(element =>
            element.matches('h1, h2, h3, [role="heading"]')
        );
        const contextRegionCandidates = visibleElements.filter(element => {
            const names = attributeNames(element).join(' ');
            const classes = safeClasses(element).join(' ');
            return /(?:contact|property|unit|detail|header)/i.test(
                `${names} ${classes}`
            );
        });
        const recognizedRoleElements = visibleElements.filter(element =>
            /^(?:tenant|vendor|owner|resident)$/i.test(renderedText(element))
        );
        const recognizedRoles = Array.from(new Set(
            recognizedRoleElements.map(element => renderedText(element).toLowerCase())
        ));
        const timestampElements = visibleElements.filter(element =>
            element.matches('time, [datetime], [data-timestamp]') ||
            /(?:^|-)(?:time|timestamp)(?:-|$)/i.test(
                safeClasses(element).join(' ')
            )
        );
        const directionalElements = visibleElements.filter(element =>
            /(?:^|-)(?:incoming|inbound|received|outgoing|outbound|sent|from-us|from-contact|left|right)(?:-|$)/i.test(
                safeClasses(element).join(' ')
            )
        );

        const repeated = new Map();
        for (const element of visibleElements) {
            const key = signature(element);
            if (!key || key === 'div||') continue;
            if (!repeated.has(key)) repeated.set(key, []);
            repeated.get(key).push(element);
        }
        const repeatedGroups = Array.from(repeated.entries())
            .filter(([_key, elements]) => elements.length >= 2)
            .map(([key, elements]) => ({
                structural_signature: key,
                element_count: elements.length,
                parent_structural_signatures: Array.from(new Set(
                    elements.map(element => element.parentElement
                        ? signature(element.parentElement) : '')
                )).filter(Boolean).slice(0, 8),
                sample_geometry: elements.slice(0, 5).map(geometry),
                sample_direct_child_counts: elements.slice(0, 8)
                    .map(element => element.children.length),
                sample_descendant_element_counts: elements.slice(0, 8)
                    .map(element => element.querySelectorAll('*').length)
            }))
            .sort((left, right) => right.element_count - left.element_count)
            .slice(0, 80);
        const repeatedSiblingStacks = [];
        for (const parent of visibleElements) {
            const children = Array.from(parent.children).filter(visible);
            if (children.length < 2) continue;
            const counts = new Map();
            for (const child of children) {
                const key = signature(child);
                counts.set(key, (counts.get(key) || 0) + 1);
            }
            const best = Array.from(counts.entries())
                .sort((left, right) => right[1] - left[1])[0];
            if (!best || best[1] < 2) continue;
            repeatedSiblingStacks.push({
                parent: compact(parent),
                repeated_child_structural_signature: best[0],
                repeated_child_count: best[1],
                visible_direct_child_count: children.length
            });
        }
        repeatedSiblingStacks.sort((left, right) =>
            right.repeated_child_count - left.repeated_child_count
        );
        const scrollContainers = visibleElements.filter(element => {
            const style = window.getComputedStyle(element);
            return element.scrollHeight > element.clientHeight + 2 &&
                /(?:auto|scroll)/.test(style.overflowY);
        }).map(element => ({
            element: describe(element),
            client_width: round(element.clientWidth),
            client_height: round(element.clientHeight),
            scroll_width: round(element.scrollWidth),
            scroll_height: round(element.scrollHeight),
            scroll_top: round(element.scrollTop),
            at_scroll_start: element.scrollTop <= 1,
            at_scroll_end: element.scrollTop + element.clientHeight >=
                element.scrollHeight - 2
        })).slice(0, 30);
        const virtualizationHints = visibleElements.filter(element => {
            const names = attributeNames(element).join(' ');
            const classes = safeClasses(element).join(' ');
            const inlineStyle = clean(element.getAttribute('style'));
            return /(?:virtual|viewport|window|sentinel)/i.test(
                `${names} ${classes}`
            ) || /transform\s*:\s*translate/i.test(inlineStyle);
        });
        const lazyHints = visibleElements.filter(element => {
            const names = attributeNames(element).join(' ');
            const classes = safeClasses(element).join(' ');
            return /(?:aria-busy|loading|lazy|sentinel|intersection)/i.test(
                `${names} ${classes}`
            );
        });
        const bubbleLike = visibleElements.filter(element =>
            /(?:bubble|message|received|sent|inbound|outbound)/i.test(
                safeClasses(element).join(' ')
            )
        );
        const legacyPredicate = {
            thread_history_present: legacyHistoryCandidates.length > 0,
            composer_present: Boolean(legacyComposer),
            send_via_sms_present: smsLabels.length > 0,
            back_control_count: backControls.length
        };
        return {
            schema_version: 1,
            sanitized: true,
            light_dom_only: true,
            viewport: {
                width: round(window.innerWidth),
                height: round(window.innerHeight)
            },
            element_counts: {
                total_light_dom_elements: all.length,
                visible_light_dom_elements: visibleElements.length,
                header_candidates: headerElements.length,
                context_region_candidates: contextRegionCandidates.length,
                composer_input_candidates: inputElements.length,
                timestamp_candidates: timestampElements.length,
                directional_indicator_candidates: directionalElements.length,
                bubble_like_candidates: bubbleLike.length
            },
            legacy_thread_predicate: legacyPredicate,
            legacy_predicate_would_accept: Boolean(
                legacyPredicate.thread_history_present &&
                legacyPredicate.composer_present &&
                legacyPredicate.send_via_sms_present &&
                legacyPredicate.back_control_count === 1
            ),
            legacy_selector_counts: {
                history_candidates: legacyHistoryCandidates.length,
                composer_candidates: legacyComposer ? 1 : 0,
                send_via_sms_labels: smsLabels.length,
                back_to_inbox_controls: backControls.length
            },
            thread_root_candidate: structuralRoot
                ? describe(structuralRoot) : null,
            thread_root_direct_children: structuralRoot
                ? Array.from(structuralRoot.children).filter(visible)
                    .slice(0, 80).map(describe)
                : [],
            back_control_structures: backControls.slice(0, 8).map(describe),
            header_structures: headerElements.slice(0, 20).map(describe),
            recognized_contact_roles: recognizedRoles,
            recognized_role_structures: recognizedRoleElements
                .slice(0, 12).map(describe),
            detail_region_structures: contextRegionCandidates
                .slice(0, 40).map(describe),
            send_via_sms_region_structures: smsLabels
                .slice(0, 8).map(describe),
            composer_region_structures: composerCandidates
                .slice(0, 16).map(describe),
            composer_input_structures: inputElements
                .slice(0, 20).map(describe),
            send_control_structures: sendControls.slice(0, 8).map(describe),
            assignment_control_count: assignmentControls.length,
            overflow_control_count: overflowControls.length,
            timestamp_structures: timestampElements.slice(0, 40).map(describe),
            direction_indicator_structures: directionalElements
                .slice(0, 40).map(describe),
            bubble_like_structures: bubbleLike.slice(0, 80).map(describe),
            repeated_structure_groups: repeatedGroups,
            repeated_sibling_stacks: repeatedSiblingStacks.slice(0, 60),
            scroll_containers: scrollContainers,
            virtualization_hint_count: virtualizationHints.length,
            virtualization_hint_structures: virtualizationHints
                .slice(0, 30).map(describe),
            older_history_lazy_load_hint_count: lazyHints.length,
            older_history_lazy_load_hint_structures: lazyHints
                .slice(0, 30).map(describe),
            older_history_loading_assessment: lazyHints.length
                ? 'structural_hints_present_not_runtime_proven'
                : 'no_static_hint_not_runtime_proven',
            visible_element_inventory: visibleElements.slice(0, 600)
                .map(compact)
        };
    }
"""


_INBOX_BACK_TO_INBOX_SCRIPT = r"""
    () => {
        // AF_INBOX_EXACT_BACK_TO_INBOX_CLICK
        const clean = value => String(value || '').trim().replace(/\s+/g, ' ');
        const controls = Array.from(document.querySelectorAll('button, a'))
            .filter(element => {
                const rect = element.getBoundingClientRect();
                const style = window.getComputedStyle(element);
                return rect.width > 0 && rect.height > 0 &&
                    style.display !== 'none' && style.visibility !== 'hidden';
            })
            .filter(element => clean(element.innerText || element.textContent)
                .toLowerCase() === 'back to inbox');
        if (controls.length !== 1) {
            return {clicked: false, match_count: controls.length};
        }
        controls[0].click();
        return {clicked: true, match_count: 1};
    }
"""


_INBOX_BACK_TO_INBOX_COUNT_SCRIPT = r"""
    () => {
        // AF_INBOX_BACK_TO_INBOX_COUNT_ONLY
        const clean = value => String(value || '').trim().replace(/\s+/g, ' ');
        const controls = Array.from(document.querySelectorAll('button, a'))
            .filter(element => {
                const rect = element.getBoundingClientRect();
                const style = window.getComputedStyle(element);
                return rect.width > 0 && rect.height > 0 &&
                    style.display !== 'none' && style.visibility !== 'hidden';
            })
            .filter(element => clean(element.innerText || element.textContent)
                .toLowerCase() === 'back to inbox');
        return {back_control_count: controls.length};
    }
"""


_INBOX_SEMANTIC_RETURN_VERIFICATION_SCRIPT = r"""
    () => {
        // AF_INBOX_SEMANTIC_RETURN_VERIFICATION_READ_ONLY
        // Structure-only. Never returns conversation text.
        const visible = element => {
            if (!(element instanceof Element)) return false;
            const rect = element.getBoundingClientRect();
            const style = window.getComputedStyle(element);
            return rect.width > 0 && rect.height > 0 &&
                style.display !== 'none' && style.visibility !== 'hidden';
        };
        const clean = value => String(value || '').trim().replace(/\s+/g, ' ');
        const exactText = (element, expected) =>
            clean(element.innerText || element.textContent).toLowerCase() ===
            expected.toLowerCase();

        const list = document.querySelector('ul.list-group');
        const listRootPresent = list instanceof Element;
        const rows = list
            ? Array.from(
                list.querySelectorAll(':scope > button > li.list-group-item')
            )
            : [];

        // A composer only exists inside an opened thread: one visible
        // textarea plus one exact "Send" button in the same container -
        // the same structural definition the thread-open readiness probe
        // requires before treating a thread as ready.
        const composerActive = Array.from(document.querySelectorAll('div'))
            .some(div =>
                Array.from(div.querySelectorAll('textarea'))
                    .filter(visible).length === 1 &&
                Array.from(div.querySelectorAll('button'))
                    .filter(visible)
                    .filter(button => exactText(button, 'Send')).length === 1
            );

        const threadDetailActive = Array.from(
            document.querySelectorAll('button, a')
        ).some(element => visible(element) && exactText(element, 'Back to Inbox'));

        return {
            list_root_present: listRootPresent,
            row_count: rows.length,
            composer_active: composerActive,
            thread_detail_active: threadDetailActive,
        };
    }
"""


def _verify_semantic_return_to_inbox(page) -> dict:
    """
    Prove the browser is genuinely back on a valid, safe inbox list view.

    Deliberately does NOT require the rendered row count to match its
    pre-open value: opening an unread conversation can legitimately mark it
    read, and AppFolio may re-sort/re-render the list as a result of that.
    Row counts are recorded here as telemetry only - never treated as
    failure by themselves. Only genuine structural drift fails this check:
    wrong route, thread-detail UI still active, a composer still active, or
    the list/filter-toolbar structure itself missing.
    """
    current_url = str(getattr(page, "url", "") or "")
    route_ok = urlsplit(current_url).path.rstrip("/") == "/texting/inbox"

    structural = None
    for frame in _discover_inbox_frames(page):
        try:
            candidate = frame.evaluate(
                _INBOX_SEMANTIC_RETURN_VERIFICATION_SCRIPT
            )
        except Exception:
            continue
        if isinstance(candidate, dict) and candidate.get("list_root_present"):
            structural = candidate
            break
    if structural is None:
        structural = {
            "list_root_present": False, "row_count": 0,
            "composer_active": False, "thread_detail_active": False,
        }

    try:
        scope_raw = page.evaluate(_INBOX_FILTER_SCOPE_RESET_SCRIPT)
    except Exception:
        scope_raw = None
    scope_status = (
        scope_raw.get("status", "conversation_list_not_found")
        if isinstance(scope_raw, dict) else "conversation_list_not_found"
    )
    # A row-count drop/rise between this reset's own before/after sampling
    # (surfaced in conversation_rows_before/after below) is deliberately
    # excluded from scope_structurally_sound - only genuine absence of the
    # list or its filter toolbar disqualifies the return.
    scope_structurally_sound = scope_status not in (
        "conversation_list_not_found", "filter_toolbar_not_found",
    )

    valid = bool(
        route_ok
        and structural.get("list_root_present")
        and not structural.get("composer_active")
        and not structural.get("thread_detail_active")
        and scope_structurally_sound
    )
    return {
        "valid": valid,
        "route_ok": route_ok,
        "current_route": urlsplit(current_url).path,
        "list_root_present": bool(structural.get("list_root_present")),
        "row_count": int(structural.get("row_count", 0) or 0),
        "composer_active": bool(structural.get("composer_active")),
        "thread_detail_active": bool(structural.get("thread_detail_active")),
        "scope_status": scope_status,
        "scope_structurally_sound": scope_structurally_sound,
        "conversation_rows_before_scope_reset": (
            int(scope_raw.get("conversation_rows_before", 0) or 0)
            if isinstance(scope_raw, dict) else 0
        ),
        "conversation_rows_after_scope_reset": (
            int(scope_raw.get("conversation_rows_after", 0) or 0)
            if isinstance(scope_raw, dict) else 0
        ),
    }


def diagnose_opened_appfolio_inbox_thread_structure(page):
    """
    Inventory the already-open thread's light DOM without returning content.

    The caller must complete exact row resolution and open authorization first.
    This probe does not click, scroll, type, submit, or persist conversation data.
    It returns the sole frame containing the exact Back to Inbox control so the
    caller can deterministically leave the thread after writing the report.
    """
    frames = _discover_inbox_frames(page)
    report = {
        "schema_version": 1,
        "sanitized": True,
        "light_dom_only": True,
        "frame_count": len(frames),
        "frames": [],
        "legacy_verified_frame_count": 0,
        "back_to_inbox_control_count": 0,
    }
    return_frame = None
    safe_route_segments = {
        "communication", "communications", "frame", "inbox", "texting",
    }
    for index, frame in enumerate(frames):
        raw_location = _safe_frame_location(frame)
        try:
            raw_path = urlsplit(raw_location).path
        except (TypeError, ValueError):
            raw_path = ""
        location_shape = "/" + "/".join(
            segment.lower()
            if segment.lower() in safe_route_segments else ":dynamic"
            for segment in raw_path.split("/")
            if segment
        )
        if location_shape == "/":
            location_shape = "unknown"
        try:
            structure = frame.evaluate(
                _INBOX_OPENED_THREAD_DOM_DIAGNOSTIC_SCRIPT
            ) or {}
            if not isinstance(structure, dict):
                raise TypeError("diagnostic result was not an object")
            _validate_sanitized_dom_diagnostic(structure)
            legacy_accepts = bool(
                structure.get("legacy_predicate_would_accept")
            )
            selector_counts = dict(
                structure.get("legacy_selector_counts") or {}
            )
            back_count = int(
                selector_counts.get("back_to_inbox_controls", 0) or 0
            )
            if legacy_accepts:
                report["legacy_verified_frame_count"] += 1
            report["back_to_inbox_control_count"] += back_count
            if back_count:
                return_frame = frame
            frame_report = {
                "frame_index": index,
                "location_shape": location_shape,
                "evaluation_status": "ok",
                "structure": structure,
            }
        except Exception as error:
            frame_report = {
                "frame_index": index,
                "location_shape": location_shape,
                "evaluation_status": "failed",
                "error_type": type(error).__name__,
                "structure": {},
            }
        report["frames"].append(frame_report)
    _validate_sanitized_dom_diagnostic(report)
    if report["back_to_inbox_control_count"] != 1 or return_frame is None:
        raise RuntimeError(
            "opened-thread diagnostic could not identify exactly one "
            "Back to Inbox control"
        )
    return report, return_frame


def _target_row_for_selector_key(
    rows: list[dict], target_selector_key: str
) -> dict:
    """Resolve one approved row without volatile timestamp/read-state input."""
    expected = str(target_selector_key or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError("target selector key must be one SHA-256 fingerprint")
    matches = []
    for raw_row in rows:
        row = dict(raw_row)
        row["event_key"] = inbox_event_key(row)
        row["selector_key"] = inbox_selector_key(row)
        if row["selector_key"] == expected:
            matches.append(row)
    if len(matches) != 1:
        raise RuntimeError(
            "exact inbox target resolution failed: "
            f"expected one list event, found {len(matches)}"
        )
    return matches[0]


def _row_match_payload(row: dict) -> dict:
    return {
        "contact_name": str(row.get("contact_name", "") or "").strip(),
        "contact_role": str(row.get("contact_role", "") or "").strip(),
        "list_detail": str(row.get("list_detail", "") or "").strip(),
        "preview": str(row.get("preview", "") or "").strip(),
        "assigned_user": str(row.get("assigned_user", "") or "").strip(),
    }


def _click_exact_inbox_row(page, row: dict) -> dict:
    """Click one proven list row only after a global uniqueness check."""
    payload = _row_match_payload(row)
    frames = _discover_inbox_frames(page)
    matches = []
    for frame in frames:
        result = frame.evaluate(_INBOX_EXACT_ROW_MATCH_SCRIPT, payload) or {}
        count = int(result.get("match_count", 0) or 0)
        if count:
            matches.append((frame, count))
    total_matches = sum(count for _frame, count in matches)
    if total_matches != 1 or len(matches) != 1:
        raise RuntimeError(
            "exact rendered inbox row match failed: "
            f"expected one row, found {total_matches}"
        )
    frame = matches[0][0]
    result = frame.evaluate(_INBOX_EXACT_ROW_CLICK_SCRIPT, payload) or {}
    if not result.get("clicked") or int(result.get("match_count", 0) or 0) != 1:
        raise RuntimeError("exact inbox row click failed closed")
    return {
        "selected_row_count": 1,
        "selected_selector_fingerprint": row["selector_key"],
        "selected_event_fingerprint": row["event_key"],
        "selected_thread_fingerprint": hashlib.sha256(
            str(row.get("thread_id", "") or "").encode("utf-8")
        ).hexdigest(),
    }


def _authorize_inbox_thread_open(
    row: dict, *, authorization: dict = None, db_path: str = None
) -> dict:
    """Require Rachel ownership or narrow explicit authority before clicking."""
    authorization = dict(authorization or {})
    kind = str(authorization.get("kind", "") or "").strip().lower()
    if not kind:
        ownership = classify_communication_ownership(row, db_path=db_path)
        if ownership.get("automatic_open_allowed"):
            return {"allowed": True, "reason": "thread_owned_by_rachel"}
        ownership_label = (
            "ownership_unknown"
            if ownership.get("classification") == "ownership_unknown"
            else "human_owned"
        )
        unread = "unread" if bool(row.get("unread")) else "read"
        raise PermissionError(
            f"thread open blocked before click: {ownership_label}_{unread}"
        )
    selector_key = str(
        authorization.get("selector_key", "") or ""
    ).strip().lower()
    if selector_key != str(row.get("selector_key", "") or "").lower():
        raise PermissionError("thread-open authorization target mismatch")

    if kind == "explicit_delegation":
        delegated_by = str(
            authorization.get("delegated_by", "") or ""
        ).strip()
        authorization_id = str(
            authorization.get("authorization_id", "") or ""
        ).strip()
        if not delegated_by or not authorization_id:
            raise PermissionError("explicit delegation evidence is incomplete")
        return {"allowed": True, "reason": "explicitly_delegated_to_rachel"}

    if kind == "rachel_started":
        durable_evidence_id = str(
            authorization.get("durable_evidence_id", "") or ""
        ).strip()
        if not durable_evidence_id:
            raise PermissionError("Rachel-started evidence is incomplete")
        return {"allowed": True, "reason": "thread_started_by_rachel"}

    if kind == "workflow_takeover":
        rule_id = str(authorization.get("rule_id", "") or "").strip()
        approved_rule_ids = {
            str(value or "").strip()
            for value in authorization.get("approved_rule_ids", [])
        }
        if not rule_id or rule_id not in approved_rule_ids:
            raise PermissionError("workflow takeover rule is not approved")
        return {"allowed": True, "reason": "workflow_authorized_takeover"}

    raise PermissionError("thread-open authorization kind is not permitted")


_THREAD_ASSERTION_CLASSIFICATIONS = {
    "unique_semantic_thread": "semantic_safety_requirement",
    "authorized_contact_role_context": "semantic_safety_requirement",
    "authorized_latest_preview": "semantic_safety_requirement",
    "history_and_composer_present": "semantic_safety_requirement",
    "message_direction_and_body": "semantic_safety_requirement",
    "same_conversation_route": "semantic_safety_requirement",
    "durable_fingerprint_uniqueness": "semantic_safety_requirement",
    "durable_upsert_and_deduplication": "semantic_safety_requirement",
    "inbox_route_scope_and_row_restoration": "semantic_safety_requirement",
    "same_browser_context": "semantic_safety_requirement",
    "zero_prohibited_actions": "semantic_safety_requirement",
    "render_readiness_polling": "react_dom_implementation_detail",
    "react_node_identity": "react_dom_implementation_detail",
    "transient_geometry": "react_dom_implementation_detail",
    "dynamic_element_references": "react_dom_implementation_detail",
    "bootstrap_layout_classes": "react_dom_implementation_detail",
    "history_wrapper_count": "diagnostic_only",
    "non_message_history_nodes": "diagnostic_only",
    "timestamp_presence": "diagnostic_only",
    "sender_metadata_presence": "diagnostic_only",
    "scroll_geometry": "diagnostic_only",
    "virtualization_and_lazy_hints": "diagnostic_only",
}


class InboxThreadPipelineError(RuntimeError):
    """Sanitized stage-specific failure for the read-only thread pipeline."""

    def __init__(self, stage: str, reason: str):
        self.stage = str(stage or "unknown_stage")
        self.reason = str(reason or "unspecified_failure")
        super().__init__(f"{self.stage}: {self.reason}")


def _record_thread_stage(
    trace: list, stage: str, *, status: str = "passed",
    reason: str = "ok", **counts
) -> None:
    entry = {
        "stage": str(stage),
        "status": str(status),
        "reason": str(reason),
    }
    for key, value in counts.items():
        if isinstance(value, bool):
            entry[str(key)] = value
        elif isinstance(value, (int, float)):
            entry[str(key)] = value
    trace.append(entry)


def _fail_thread_stage(trace: list, stage: str, reason: str):
    _record_thread_stage(
        trace, stage, status="failed", reason=reason
    )
    raise InboxThreadPipelineError(stage, reason)


def _normalized_thread_value(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().lower()


def _thread_route_fingerprint(page) -> str:
    try:
        path = urlsplit(str(getattr(page, "url", "") or "")).path
    except (TypeError, ValueError):
        path = ""
    return hashlib.sha256(path.encode("utf-8")).hexdigest() if path else ""


def _thread_parse_failure_reasons(result: dict) -> list[str]:
    reasons = [
        str(value) for value in result.get("structure_failure_reasons", [])
        if re.fullmatch(r"[a-z0-9_]+", str(value or ""))
    ]
    if int(result.get("thread_root_count", 0) or 0) != 1:
        reasons.append("thread_root_count_invalid")
    if int(result.get("header_region_count", 0) or 0) != 1:
        reasons.append("header_region_count_invalid")
    if int(result.get("history_region_count", 0) or 0) != 1:
        reasons.append("history_region_count_invalid")
    if int(result.get("composer_region_count", 0) or 0) != 1:
        reasons.append("composer_region_count_invalid")
    if int(result.get("rendered_message_item_count", 0) or 0) < 1:
        reasons.append("message_list_empty")
    if not bool(result.get("message_structure_valid")):
        reasons.append("message_structure_invalid")
    if int(result.get("rendered_message_item_count", 0) or 0) != int(
        result.get("message_count", 0) or 0
    ):
        reasons.append("parsed_message_count_mismatch")
    for key, count in dict(result.get("message_failure_counts") or {}).items():
        if int(count or 0) > 0 and re.fullmatch(r"[a-z0-9_]+", str(key)):
            reasons.append(f"message_{key}")
    return sorted(set(reasons))


def _verify_thread_identity(selected_row: dict, thread: dict) -> dict:
    compared = 0
    matched = 0
    expected_contact = _normalized_thread_value(
        selected_row.get("contact_name")
    )
    opened_contact = _normalized_thread_value(thread.get("contact_name"))
    if not expected_contact or not opened_contact:
        raise InboxThreadPipelineError(
            "identity_verified", "contact_identity_unavailable"
        )
    compared += 1
    if expected_contact != opened_contact:
        raise InboxThreadPipelineError(
            "identity_verified", "contact_identity_mismatch"
        )
    matched += 1

    expected_role = _normalized_thread_value(selected_row.get("contact_role"))
    opened_role = _normalized_thread_value(thread.get("contact_role"))
    if expected_role:
        compared += 1
        if expected_role != opened_role:
            raise InboxThreadPipelineError(
                "identity_verified", "contact_role_mismatch"
            )
        matched += 1

    detail_context = _normalized_thread_value(thread.get("detail_context"))
    for key in ("property", "unit"):
        expected = _normalized_thread_value(selected_row.get(key))
        if not expected:
            continue
        compared += 1
        if expected not in detail_context:
            raise InboxThreadPipelineError(
                "identity_verified", f"{key}_context_mismatch"
            )
        matched += 1
    return {
        "identity_fields_compared": compared,
        "identity_fields_matched": matched,
    }


def _latest_preview_matches(selected_row: dict, messages: list[dict]) -> bool:
    expected = _normalized_thread_value(selected_row.get("preview"))
    expected = expected.rstrip(". …")
    if not expected or not messages:
        return False
    latest = _normalized_thread_value(messages[-1].get("message_text"))
    return bool(expected and latest and (
        expected in latest or latest in expected
    ))


def _read_verified_thread(
    page, selected_row: dict, *, stage_trace=None
) -> dict:
    """Retry transient React renders; accept only one semantic thread state."""
    trace = stage_trace if stage_trace is not None else []
    last_reasons = ["thread_not_ready"]
    for attempt in range(20):
        ready_frames = []
        for frame in _discover_inbox_frames(page):
            try:
                readiness = frame.evaluate(_INBOX_THREAD_READY_SCRIPT) or {}
            except Exception:
                last_reasons = ["readiness_evaluation_unavailable"]
                continue
            ready_count = int(
                readiness.get("ready_thread_root_count", 0) or 0
            )
            if ready_count > 1:
                _fail_thread_stage(
                    trace, "thread_ready", "ambiguous_thread_roots"
                )
            if (
                ready_count == 1 and bool(readiness.get("thread_ready"))
                and int(readiness.get("rendered_message_item_count", 0) or 0) > 0
            ):
                ready_frames.append(frame)
        if len(ready_frames) > 1:
            _fail_thread_stage(
                trace, "thread_ready", "ambiguous_ready_frames"
            )
        if len(ready_frames) == 1:
            frame = ready_frames[0]
            try:
                result = dict(
                    frame.evaluate(_INBOX_THREAD_READ_SCRIPT) or {}
                )
            except Exception:
                last_reasons = ["parser_evaluation_unavailable"]
                if attempt < 19:
                    try:
                        page.wait_for_timeout(250)
                    except Exception:
                        pass
                continue
            reasons = _thread_parse_failure_reasons(result)
            if not reasons:
                try:
                    identity = _verify_thread_identity(selected_row, result)
                except InboxThreadPipelineError as error:
                    _fail_thread_stage(trace, error.stage, error.reason)
                if not _latest_preview_matches(
                    selected_row, list(result.get("messages") or [])
                ):
                    _fail_thread_stage(
                        trace, "identity_verified",
                        "authorized_latest_preview_mismatch",
                    )
                result.update(identity)
                result["readiness_attempts"] = attempt + 1
                result["verified_frame"] = frame
                result["conversation_route_fingerprint"] = (
                    _thread_route_fingerprint(page)
                )
                _record_thread_stage(
                    trace, "thread_ready",
                    reason="single_semantic_thread_ready",
                    readiness_attempts=attempt + 1,
                )
                _record_thread_stage(
                    trace, "identity_verified",
                    reason="authorized_context_matched",
                    fields_compared=identity["identity_fields_compared"],
                )
                _record_thread_stage(
                    trace, "history_verified",
                    reason="semantic_history_and_composer_present",
                    message_items=int(
                        result.get("rendered_message_item_count", 0) or 0
                    ),
                )
                _record_thread_stage(
                    trace, "messages_parsed",
                    reason="all_semantic_message_items_parsed",
                    messages=int(result.get("message_count", 0) or 0),
                )
                return result
            if "ambiguous_thread_roots" in reasons:
                _fail_thread_stage(
                    trace, "thread_ready", "ambiguous_thread_roots"
                )
            last_reasons = reasons
        if attempt < 19:
            try:
                page.wait_for_timeout(250)
            except Exception:
                pass
    reason = "render_timeout__" + "__".join(last_reasons[:6])
    _fail_thread_stage(trace, "thread_ready", reason)


def _normalize_thread_messages(
    thread_id: str, messages: list[dict]
) -> list[dict]:
    """Fingerprint messages independently of volatile rendered timestamps."""
    occurrence_counts = {}
    normalized = []
    for index, raw_message in enumerate(messages):
        message = dict(raw_message or {})
        text_value = re.sub(
            r"\s+", " ", str(message.get("message_text", "") or "")
        ).strip()
        if not text_value:
            continue
        direction = str(message.get("direction", "") or "unknown").strip()
        source_message_id = str(
            message.get("source_message_id", "") or ""
        ).strip()
        base_identity = "|".join(
            (thread_id, "source-id", source_message_id)
            if source_message_id else
            (thread_id, "rendered", direction, text_value)
        )
        occurrence = occurrence_counts.get(base_identity, 0)
        occurrence_counts[base_identity] = occurrence + 1
        fingerprint = hashlib.sha256(
            f"{base_identity}|occurrence={occurrence}".encode("utf-8")
        ).hexdigest()
        message.update({
            "message_text": text_value,
            "direction": direction,
            "source_message_id": (
                source_message_id or f"thread-message-{fingerprint}"
            ),
            "message_fingerprint": fingerprint,
            "rendered_index": int(message.get("rendered_index", index) or 0),
        })
        normalized.append(message)
    return normalized


def _store_verified_thread(
    selected_row: dict, thread: dict, *, db_path: str
) -> dict:
    messages = list(thread.get("normalized_messages") or [])
    if not messages:
        messages = _normalize_thread_messages(
            str(selected_row.get("thread_id", "") or ""),
            list(thread.get("messages") or []),
        )
    source = "appfolio_communication_inbox_thread"
    thread_id = str(selected_row.get("thread_id", "") or "")
    ownership = classify_communication_ownership(
        selected_row, db_path=db_path
    )
    initialize_event_memory(db_path)
    # conversation_identity_key is the durable logical-conversation linkage
    # going forward; source_thread_id (the legacy synthetic/native id) is
    # preserved on every row exactly as before, for audit/backward
    # compatibility. resolve_and_link_conversation_identity is idempotent
    # and, once linked, keeps returning the same canonical identity for
    # this legacy_thread_id even if a later read computes a different
    # fresh identity key - preventing a fragmented second history.
    raw_identity_key = str(
        selected_row.get("conversation_identity_key", "")
        or conversation_identity_key(selected_row)
    )
    canonical_identity_key = resolve_and_link_conversation_identity(
        conversation_identity_key=raw_identity_key,
        legacy_thread_id=thread_id,
        db_path=db_path,
    )
    backfilled_legacy_rows = backfill_conversation_identity_for_legacy_thread(
        conversation_identity_key=canonical_identity_key,
        legacy_thread_id=thread_id,
        db_path=db_path,
    )
    conn = sqlite3.connect(db_path)
    try:
        existing_source_ids = {
            str(row[0] or "")
            for row in conn.execute(
                "SELECT source_message_id FROM communications "
                "WHERE source = ? AND "
                "(conversation_identity_key = ? OR source_thread_id = ?)",
                (source, canonical_identity_key, thread_id),
            ).fetchall()
        }
    finally:
        conn.close()
    communication_ids = []
    related_wo_count = 0
    for message in messages:
        related = find_related_work_order(
            message_text=message["message_text"],
            contact_name=selected_row.get("contact_name", ""),
            contact_role=selected_row.get("contact_role", ""),
            property_address=selected_row.get("property", ""),
            unit=selected_row.get("unit", ""),
            db_path=db_path,
        )
        if related:
            related_wo_count += 1
        communication_ids.append(store_communication(
            {
                "source": source,
                "source_thread_id": thread_id,
                "conversation_identity_key": canonical_identity_key,
                "source_message_id": message["source_message_id"],
                "contact_name": selected_row.get("contact_name", ""),
                "contact_role": selected_row.get("contact_role", ""),
                "property_address": selected_row.get("property", ""),
                "unit": selected_row.get("unit", ""),
                "occurred_at": message.get("timestamp", ""),
                "direction": message.get("direction", "unknown"),
                "message_text": message["message_text"],
                "related_wo": related.get("wo_num", "") if related else "",
                "related_tenant": (
                    selected_row.get("contact_name", "")
                    if str(selected_row.get("contact_role", "")).lower()
                    in {"tenant", "resident"} else ""
                ),
                "related_vendor": (
                    selected_row.get("contact_name", "")
                    if str(selected_row.get("contact_role", "")).lower()
                    == "vendor" else ""
                ),
                "internal_user_id": ownership.get(
                    "owner_internal_user_id", ""
                ),
                "processing_status": "thread_read_verified",
                "metadata": {
                    "message_fingerprint": message["message_fingerprint"],
                    "rendered_index": int(
                        message.get("rendered_index", 0) or 0
                    ),
                    "sender_classification": message.get(
                        "sender_classification", ""
                    ),
                    "opened_thread_phone": thread.get("phone", ""),
                    "opened_thread_headings": thread.get("headings", []),
                    "opened_thread_context_fields": thread.get(
                        "context_fields", []
                    ),
                    "thread_opened": True,
                    "source_scope": "verified_single_thread_read",
                    "ownership_classification": ownership.get(
                        "classification", "ownership_unknown"
                    ),
                    "ownership_reason": ownership.get("reason", ""),
                },
            },
            db_path,
        ))
    unique_communication_ids = set(communication_ids)
    durable_rows_after = 0
    if unique_communication_ids:
        placeholders = ",".join("?" for _value in unique_communication_ids)
        conn = sqlite3.connect(db_path)
        try:
            durable_rows_after = int(conn.execute(
                f"SELECT COUNT(*) FROM communications "
                f"WHERE communication_id IN ({placeholders})",
                tuple(unique_communication_ids),
            ).fetchone()[0])
        finally:
            conn.close()
    record_communication_state(
        selected_row,
        "thread_read_verified",
        communication_id=communication_ids[-1] if communication_ids else "",
        db_path=db_path,
    )
    return {
        "messages_parsed": len(messages),
        "messages_stored": len(communication_ids),
        "messages_fingerprinted": len(messages),
        "unique_message_fingerprints": len({
            message["message_fingerprint"] for message in messages
        }),
        "messages_previously_stored": sum(
            1 for message in messages
            if message["source_message_id"] in existing_source_ids
        ),
        "messages_newly_stored": sum(
            1 for message in messages
            if message["source_message_id"] not in existing_source_ids
        ),
        "durable_message_rows_after": durable_rows_after,
        "message_deduplication_verified": bool(
            durable_rows_after == len(unique_communication_ids)
            and len(unique_communication_ids) == len(messages)
        ),
        "explicit_wo_relationships": related_wo_count,
        "verified_wo_relationships": related_wo_count,
        "conversation_identity_key": canonical_identity_key,
        "legacy_thread_id": thread_id,
        "backfilled_legacy_message_rows": backfilled_legacy_rows,
    }


def _semantic_thread_postcheck(
    page, selected_row: dict, original_thread: dict,
    normalized_messages: list[dict], *, stage_trace=None
) -> dict:
    """Re-query semantic state; never compare retained React DOM nodes."""
    trace = stage_trace if stage_trace is not None else []
    original_route = str(
        original_thread.get("conversation_route_fingerprint", "") or ""
    )
    original_fingerprints = [
        message["message_fingerprint"] for message in normalized_messages
    ]
    last_reason = "semantic_thread_temporarily_unavailable"
    for attempt in range(12):
        candidates = []
        for frame in _discover_inbox_frames(page):
            try:
                result = dict(
                    frame.evaluate(_INBOX_THREAD_READ_SCRIPT) or {}
                )
            except Exception:
                last_reason = "postcheck_evaluation_unavailable"
                continue
            reasons = _thread_parse_failure_reasons(result)
            if int(result.get("thread_root_count", 0) or 0) > 1:
                _fail_thread_stage(
                    trace, "semantic_postcheck_passed",
                    "ambiguous_thread_roots",
                )
            if not reasons:
                candidates.append(result)
            elif reasons:
                last_reason = "__".join(reasons[:6])
        if len(candidates) > 1:
            _fail_thread_stage(
                trace, "semantic_postcheck_passed",
                "ambiguous_semantic_thread_states",
            )
        if len(candidates) == 1:
            current = candidates[0]
            try:
                identity = _verify_thread_identity(selected_row, current)
            except InboxThreadPipelineError as error:
                _fail_thread_stage(
                    trace, "semantic_postcheck_passed", error.reason
                )
            if not _latest_preview_matches(
                selected_row, list(current.get("messages") or [])
            ):
                _fail_thread_stage(
                    trace, "semantic_postcheck_passed",
                    "authorized_latest_preview_changed",
                )
            current_route = _thread_route_fingerprint(page)
            if not original_route or current_route != original_route:
                _fail_thread_stage(
                    trace, "semantic_postcheck_passed",
                    "conversation_route_changed",
                )
            current_messages = _normalize_thread_messages(
                str(selected_row.get("thread_id", "") or ""),
                list(current.get("messages") or []),
            )
            current_fingerprints = [
                message["message_fingerprint"]
                for message in current_messages
            ]
            if current_fingerprints != original_fingerprints:
                _fail_thread_stage(
                    trace, "semantic_postcheck_passed",
                    "conversation_messages_changed",
                )
            _record_thread_stage(
                trace, "semantic_postcheck_passed",
                reason="semantic_state_reverified_without_node_identity",
                attempts=attempt + 1,
                identity_fields=identity["identity_fields_compared"],
                messages=len(current_messages),
            )
            return current
        if attempt < 11:
            try:
                page.wait_for_timeout(250)
            except Exception:
                pass
    _fail_thread_stage(
        trace, "semantic_postcheck_passed",
        f"semantic_postcheck_timeout__{last_reason}",
    )


def _return_to_inbox(page, thread: dict = None) -> int:
    clicked = False
    for attempt in range(20):
        matches = []
        for candidate in _discover_inbox_frames(page):
            try:
                result = candidate.evaluate(
                    _INBOX_BACK_TO_INBOX_COUNT_SCRIPT
                ) or {}
            except Exception:
                continue
            count = int(result.get("back_control_count", 0) or 0)
            if count:
                matches.append((candidate, count))
        total_matches = sum(count for _candidate, count in matches)
        if total_matches > 1:
            raise InboxThreadPipelineError(
                "returned_to_inbox", "ambiguous_back_controls"
            )
        if total_matches == 1 and len(matches) == 1:
            try:
                result = matches[0][0].evaluate(
                    _INBOX_BACK_TO_INBOX_SCRIPT
                ) or {}
            except Exception:
                result = {}
            click_matches = int(result.get("match_count", 0) or 0)
            if click_matches > 1:
                raise InboxThreadPipelineError(
                    "returned_to_inbox", "ambiguous_back_controls"
                )
            if result.get("clicked") and click_matches == 1:
                clicked = True
                break
        if attempt < 19:
            try:
                page.wait_for_timeout(250)
            except Exception:
                pass
    if not clicked:
        raise InboxThreadPipelineError(
            "returned_to_inbox", "back_control_render_timeout"
        )
    for attempt in range(20):
        current_url = str(getattr(page, "url", "") or "")
        if urlsplit(current_url).path.rstrip("/") == "/texting/inbox":
            return attempt + 1
        if attempt < 19:
            try:
                page.wait_for_timeout(250)
            except Exception:
                pass
    raise InboxThreadPipelineError(
        "returned_to_inbox", "inbox_route_restoration_timeout"
    )


def inspect_one_appfolio_inbox_thread_read_only(
    page, *, target_selector_key: str, expected_event_key: str = "",
    authorization: dict = None, db_path=None, metrics=None
) -> dict:
    """Execute the complete authorized, read-only individual-thread pipeline."""
    started = time.monotonic()
    read_db_path = db_path or DB_PATH
    stage_trace = []
    result_metrics = {
        "rows_seen": 0,
        "selected_row_count": 0,
        "conversation_opens": 0,
        "messages_parsed": 0,
        "messages_stored": 0,
        "messages_fingerprinted": 0,
        "unique_message_fingerprints": 0,
        "messages_previously_stored": 0,
        "messages_newly_stored": 0,
        "durable_message_rows_after": 0,
        "message_deduplication_verified": False,
        "explicit_wo_relationships": 0,
        "unread_before": False,
        "unread_after": False,
        "unread_to_read_transition": False,
        "opening_treated_as_global_read_mutation": True,
        "expected_event_fingerprint_matched": False,
        "volatile_timestamp_present": False,
        "open_authorization_reason": "",
        "thread_history_present": False,
        "send_via_sms_present": False,
        "back_to_inbox_present": False,
        "history_scrollable": False,
        "history_at_scroll_start": False,
        "history_at_scroll_end": False,
        "virtualization_hints_present": False,
        "lazy_load_hints_present": False,
        "rendered_message_item_count": 0,
        "invalid_message_structure_count": 0,
        "readiness_attempts": 0,
        "filter_scope_reverified": False,
        "returned_to_inbox": False,
        "unique_rows_seen": 0,
        "target_resolution_iterations": 0,
        "target_resolution_end_condition": "",
        "target_match_count": 0,
        "conversation_identity_key": "",
        "conversation_state_key": "",
        "return_verification": None,
        "appfolio_writes_attempted": 0,
        "outbound_messages_attempted": 0,
        "composer_inputs_attempted": 0,
        "read_state_toggles_attempted": 0,
        "assignment_changes_attempted": 0,
        "overflow_actions_attempted": 0,
        "scheduling_actions_attempted": 0,
        "stage_trace": stage_trace,
        "failure_stage": "",
        "failure_reason": "",
        "duration_seconds": 0.0,
    }
    selected = None
    thread = {}
    clicked = False
    pipeline_error = None
    active_stage = "target_resolved"
    thread = {}
    try:
        AppFolioReadAdapter(
            page, "communication_thread_reader"
        ).navigate_read(
            INBOX_URL,
            route_pattern="/texting/inbox",
            timeout=30000,
            attempts=2,
        )
        human_delay(2, 3)
        scope = ensure_appfolio_inbox_full_monitoring_scope(page)
        if not scope.get("verified_full_scope"):
            _fail_thread_stage(
                stage_trace, "target_resolved", "full_scope_unverified"
            )
        traversal = resolve_target_row_via_full_inbox_traversal(
            page, target_selector_key
        )
        result_metrics["rows_seen"] = traversal["initial_rows"]
        result_metrics["unique_rows_seen"] = traversal["unique_rows_seen"]
        result_metrics["target_resolution_iterations"] = (
            traversal["iterations"]
        )
        result_metrics["target_resolution_end_condition"] = (
            traversal["end_condition"]
        )
        result_metrics["target_match_count"] = traversal["target_match_count"]
        if not traversal["target_found"]:
            _fail_thread_stage(
                stage_trace, "target_resolved",
                traversal["end_condition"] or "exact_selector_resolution_failed",
            )
        selected = traversal["matched_row"]
        _record_thread_stage(
            stage_trace, "target_resolved",
            reason="one_stable_selector_match",
            rows_seen=traversal["initial_rows"],
            unique_rows_seen=traversal["unique_rows_seen"],
            traversal_iterations=traversal["iterations"],
        )
        result_metrics["conversation_identity_key"] = str(
            selected.get("conversation_identity_key", "") or ""
        )
        result_metrics["conversation_state_key"] = str(
            selected.get("conversation_state_key", "") or ""
        )
        result_metrics["volatile_timestamp_present"] = bool(
            selected.get("timestamp")
        )
        normalized_expected_event_key = str(
            expected_event_key or ""
        ).strip().lower()
        result_metrics["expected_event_fingerprint_matched"] = bool(
            normalized_expected_event_key
            and normalized_expected_event_key == selected.get("event_key")
        )
        result_metrics["unread_before"] = bool(selected.get("unread"))

        active_stage = "authorization_verified"
        try:
            open_authorization = _authorize_inbox_thread_open(
                selected, authorization=authorization,
                db_path=read_db_path,
            )
        except Exception:
            _fail_thread_stage(
                stage_trace, active_stage, "authorization_denied"
            )
        result_metrics["open_authorization_reason"] = (
            open_authorization["reason"]
        )
        _record_thread_stage(
            stage_trace, active_stage,
            reason="event_specific_open_authority_verified",
        )

        active_stage = "thread_clicked"
        try:
            evidence = _click_exact_inbox_row(page, selected)
        except Exception:
            _fail_thread_stage(
                stage_trace, active_stage, "exact_row_click_failed"
            )
        clicked = True
        result_metrics.update(evidence)
        result_metrics["conversation_opens"] = 1
        _record_thread_stage(
            stage_trace, active_stage,
            reason="exact_authorized_row_clicked", conversation_opens=1,
        )

        active_stage = "thread_ready"
        thread = _read_verified_thread(
            page, selected, stage_trace=stage_trace
        )
        result_metrics.update({
            "thread_history_present": bool(thread.get("thread_history_present")),
            "send_via_sms_present": bool(thread.get("send_via_sms_present")),
            "back_to_inbox_present": int(
                thread.get("back_control_count", 0) or 0
            ) == 1,
            "history_scrollable": bool(thread.get("history_scrollable")),
            "history_at_scroll_start": bool(
                thread.get("history_at_scroll_start")
            ),
            "history_at_scroll_end": bool(thread.get("history_at_scroll_end")),
            "virtualization_hints_present": bool(
                thread.get("virtualization_hints_present")
            ),
            "lazy_load_hints_present": bool(
                thread.get("lazy_load_hints_present")
            ),
            "rendered_message_item_count": int(
                thread.get("rendered_message_item_count", 0) or 0
            ),
            "invalid_message_structure_count": int(
                thread.get("invalid_message_structure_count", 0) or 0
            ),
            "readiness_attempts": int(
                thread.get("readiness_attempts", 0) or 0
            ),
            "identity_fields_compared": int(
                thread.get("identity_fields_compared", 0) or 0
            ),
            "identity_fields_matched": int(
                thread.get("identity_fields_matched", 0) or 0
            ),
        })

        active_stage = "fingerprints_created"
        normalized_messages = _normalize_thread_messages(
            str(selected.get("thread_id", "") or ""),
            list(thread.get("messages") or []),
        )
        raw_message_count = len(list(thread.get("messages") or []))
        unique_fingerprints = {
            message["message_fingerprint"]
            for message in normalized_messages
        }
        if len(normalized_messages) != raw_message_count:
            _fail_thread_stage(
                stage_trace, active_stage,
                "fingerprint_input_count_mismatch",
            )
        if len(unique_fingerprints) != len(normalized_messages):
            _fail_thread_stage(
                stage_trace, active_stage,
                "duplicate_message_fingerprints",
            )
        if any(
            message.get("direction") not in {"inbound", "outbound"}
            for message in normalized_messages
        ):
            _fail_thread_stage(
                stage_trace, active_stage,
                "unclassified_message_direction",
            )
        thread["normalized_messages"] = normalized_messages
        _record_thread_stage(
            stage_trace, active_stage,
            reason="stable_timestamp_independent_fingerprints_created",
            messages=len(normalized_messages),
        )

        active_stage = "durable_upsert_complete"
        try:
            storage = _store_verified_thread(
                selected, thread, db_path=read_db_path
            )
        except Exception as error:
            _fail_thread_stage(
                stage_trace, active_stage,
                f"durable_upsert_failed_{type(error).__name__}",
            )
        result_metrics.update(storage)
        if not storage.get("message_deduplication_verified"):
            _fail_thread_stage(
                stage_trace, active_stage,
                "durable_deduplication_postcheck_failed",
            )
        _record_thread_stage(
            stage_trace, active_stage,
            reason="idempotent_durable_upsert_verified",
            messages=int(storage.get("messages_stored", 0) or 0),
            new_messages=int(storage.get("messages_newly_stored", 0) or 0),
            existing_messages=int(
                storage.get("messages_previously_stored", 0) or 0
            ),
        )

        active_stage = "semantic_postcheck_passed"
        _semantic_thread_postcheck(
            page, selected, thread, normalized_messages,
            stage_trace=stage_trace,
        )
    except InboxThreadPipelineError as error:
        pipeline_error = error
    except Exception as error:
        reason = f"unexpected_{type(error).__name__}"
        _record_thread_stage(
            stage_trace, active_stage, status="failed", reason=reason
        )
        pipeline_error = InboxThreadPipelineError(active_stage, reason)
    finally:
        if clicked:
            active_stage = "returned_to_inbox"
            try:
                return_attempts = _return_to_inbox(page, thread)
                verification = _verify_semantic_return_to_inbox(page)
                result_metrics["return_verification"] = verification
                if not verification["valid"]:
                    failed_checks = sorted(
                        key for key, ok in (
                            ("route", verification["route_ok"]),
                            ("list_root", verification["list_root_present"]),
                            (
                                "composer_inactive",
                                not verification["composer_active"],
                            ),
                            (
                                "thread_detail_inactive",
                                not verification["thread_detail_active"],
                            ),
                            (
                                "scope_structurally_sound",
                                verification["scope_structurally_sound"],
                            ),
                        ) if not ok
                    )
                    raise InboxThreadPipelineError(
                        active_stage,
                        "semantic_return_verification_failed:"
                        + ",".join(failed_checks),
                    )
                returned_rows = _read_lightweight_inbox_rows(page)
                same_thread_rows = [
                    row for row in returned_rows
                    if row.get("thread_id") == selected.get("thread_id")
                ]
                if len(same_thread_rows) != 1:
                    raise InboxThreadPipelineError(
                        active_stage, "selected_thread_not_restored_in_list"
                    )
                result_metrics["unread_after"] = bool(
                    same_thread_rows[0].get("unread")
                )
                result_metrics["unread_to_read_transition"] = bool(
                    result_metrics["unread_before"]
                    and not result_metrics["unread_after"]
                )
                result_metrics["filter_scope_reverified"] = True
                result_metrics["returned_to_inbox"] = True
                _record_thread_stage(
                    stage_trace, active_stage,
                    reason="inbox_route_scope_and_thread_restored",
                    attempts=return_attempts,
                )
            except InboxThreadPipelineError as error:
                _record_thread_stage(
                    stage_trace, active_stage, status="failed",
                    reason=error.reason,
                )
                if pipeline_error is None:
                    pipeline_error = error
                else:
                    pipeline_error = InboxThreadPipelineError(
                        pipeline_error.stage,
                        f"{pipeline_error.reason}__return_cleanup_failed",
                    )
            except Exception as error:
                reason = f"return_failed_{type(error).__name__}"
                _record_thread_stage(
                    stage_trace, active_stage, status="failed", reason=reason
                )
                if pipeline_error is None:
                    pipeline_error = InboxThreadPipelineError(
                        active_stage, reason
                    )
        result_metrics["duration_seconds"] = round(
            time.monotonic() - started, 2
        )
        if pipeline_error is not None:
            result_metrics["failure_stage"] = pipeline_error.stage
            result_metrics["failure_reason"] = pipeline_error.reason
        assert_zero_write_metrics(
            result_metrics, context="communication_thread_read_only"
        )
        if metrics is not None:
            metrics.clear()
            metrics.update(result_metrics)
        sanitized = {
            key: value for key, value in result_metrics.items()
            if key not in {
                "selected_selector_fingerprint", "selected_event_fingerprint",
                "selected_thread_fingerprint",
            }
        }
        print(
            "[AF-InboxThreadStageTrace] "
            + json.dumps(stage_trace, sort_keys=True)
        )
        print(
            "[AF-InboxThreadReadMetrics] "
            + json.dumps(sanitized, sort_keys=True)
        )
    if pipeline_error is not None:
        raise pipeline_error
    return result_metrics


def _read_thread_event(page) -> dict:
    return page.evaluate(
        """
        () => {
            const messages = [];
            const bubbles = document.querySelectorAll(
                '[class*="message"], [class*="bubble"], div[class*="sms"], div[class*="text-message"]'
            );
            for (const bubble of bubbles) {
                const text = (bubble.innerText || bubble.textContent || '').trim();
                if (text.length < 2) continue;
                const sent = bubble.classList.contains('sent') ||
                    bubble.classList.contains('outgoing') ||
                    Boolean(bubble.querySelector('[class*="sent"]')) ||
                    text.includes('Rachel Asst');
                const timeElement = bubble.querySelector('time, [datetime], [class*="date"], [class*="time"]');
                messages.push({
                    text: text.substring(0, 2000),
                    sent: sent,
                    timestamp: timeElement ? timeElement.textContent.trim().substring(0, 120) : '',
                    source_message_id: bubble.dataset.messageId || bubble.dataset.id || ''
                });
            }
            const header = document.querySelector('h1, h2, h3, [class*="contact"]');
            const headerText = header ? header.textContent.trim() : '';
            const phoneElement = document.querySelector('a[href*="tel:"], [class*="phone"]');
            return {
                sender: headerText.substring(0, 160),
                phone: phoneElement ? phoneElement.textContent.trim().substring(0, 80) : '',
                messages: messages
            };
        }
        """
    ) or {}


def _route_linked_communication(page, wo_row: dict, message: str) -> bool:
    status = wo_row.get("status", "")
    wo_num = wo_row.get("wo_num", "")
    if status == "satisfaction_check_sent":
        from rachel_vendor_lifecycle import handle_tenant_satisfaction_reply
        handle_tenant_satisfaction_reply(wo_num, message)
        return True
    if status == "awaiting_reply":
        return bool(process_triage_reply(page, wo_row, message))
    if status in ("waiting_for_tenant_availability", "waiting_for_tenant_confirmation"):
        from rachel_vendor_lifecycle import handle_tenant_schedule_reply
        handle_tenant_schedule_reply(wo_num, message)
        return True
    return False


def check_appfolio_inbox(
    page, *, db_path=None, metrics=None
) -> int:
    """Store new list events without opening or changing a conversation."""
    started = time.monotonic()
    poll_db_path = db_path or DB_PATH
    AppFolioReadAdapter(page, "communication_inbox").navigate_read(
        INBOX_URL,
        route_pattern="/texting/inbox",
        timeout=30000,
        attempts=2,
    )
    human_delay(2, 3)
    filter_scope = ensure_appfolio_inbox_full_monitoring_scope(page)
    rows = _read_lightweight_inbox_rows(page)
    unseen = unseen_communication_rows(rows, poll_db_path)
    unseen_event_keys = [str(row.get("event_key", "")) for row in unseen]
    duplicate_event_fingerprints = len(unseen_event_keys) - len(
        set(unseen_event_keys)
    )
    print(
        f"[AF-InboxMetrics] polls=1 rows_seen={len(rows)} "
        f"new_unseen={len(unseen)}"
    )
    stored_count = 0
    failed_count = 0
    threads_opened = 0
    ownership_counts = {}
    routing_action_counts = {}
    for row in unseen:
        record_communication_state(row, "processing", db_path=poll_db_path)
        try:
            ownership = classify_communication_ownership(
                row, db_path=poll_db_path
            )
            ownership_classification = str(
                ownership.get("classification", "ownership_unknown")
            )
            routing_action = str(
                ownership.get("routing_action", "manual_ownership_review")
            )
            ownership_counts[ownership_classification] = (
                ownership_counts.get(ownership_classification, 0) + 1
            )
            routing_action_counts[routing_action] = (
                routing_action_counts.get(routing_action, 0) + 1
            )
            message_text = str(row.get("preview", "") or "").strip()
            sender_name = str(
                row.get("contact_name", "") or ""
            ).strip()
            role = str(row.get("contact_role", "") or "").strip()
            related = find_related_work_order(
                message_text=message_text,
                contact_name=sender_name,
                contact_role=role,
                property_address=row.get("property", ""),
                unit=row.get("unit", ""),
                db_path=poll_db_path,
            )
            list_legacy_thread_id = str(row.get("thread_id", "") or "")
            list_identity_key = resolve_and_link_conversation_identity(
                conversation_identity_key=str(
                    row.get("conversation_identity_key", "")
                    or conversation_identity_key(row)
                ),
                legacy_thread_id=list_legacy_thread_id,
                db_path=poll_db_path,
            )
            communication_id = store_communication(
                {
                    "source": "appfolio_communication_inbox",
                    "source_thread_id": list_legacy_thread_id,
                    "conversation_identity_key": list_identity_key,
                    "source_message_id": row.get("source_message_id", ""),
                    "contact_name": sender_name,
                    "contact_role": role,
                    "property_address": row.get("property", ""),
                    "unit": row.get("unit", ""),
                    "occurred_at": row.get("timestamp", ""),
                    "direction": "inbound",
                    "message_text": message_text,
                    "preview": row.get("preview", ""),
                    "related_wo": related.get("wo_num", "") if related else "",
                    "related_tenant": related.get("tenant", "") if related else (sender_name if role in ("tenant", "resident") else ""),
                    "related_vendor": related.get("vendor_name", "") if related else (sender_name if role == "vendor" else ""),
                    "internal_user_id": ownership.get(
                        "owner_internal_user_id", ""
                    ),
                    "processing_status": "list_observed_unopened",
                    "metadata": {
                        "unread": bool(row.get("unread")),
                        "assigned_user": row.get("assigned_user", ""),
                        "thread_opened": False,
                        "source_scope": "inbox_list_only",
                        "ownership_classification": ownership_classification,
                        "portfolio_access_verified": bool(
                            ownership.get("portfolio_access_verified")
                        ),
                        "routing_action": routing_action,
                        "automatic_open_allowed": bool(
                            ownership.get("automatic_open_allowed")
                        ),
                        "ownership_reason": ownership.get("reason", ""),
                    },
                },
                poll_db_path,
            )
            record_communication_state(
                row, "list_observed_unopened",
                communication_id=communication_id, db_path=poll_db_path,
            )
            stored_count += 1
        except Exception as error:
            failed_count += 1
            record_communication_state(
                row, "failed", error_type=type(error).__name__,
                db_path=poll_db_path,
            )
            print(f"[AF-Inbox] Event processing failed: {type(error).__name__}")
    print(
        f"[AF-InboxMetrics] threads_opened={threads_opened} stored={stored_count} "
        f"duration_seconds={time.monotonic() - started:.2f}"
    )
    poll_metrics = {
        "filter_status": filter_scope.get("status", "unknown"),
        "filter_verified": bool(filter_scope.get("verified_full_scope")),
        "rows_seen": len(rows),
        "newly_observed": len(unseen),
        "already_observed": len(rows) - len(unseen),
        "stored_events": stored_count,
        "failed_events": failed_count,
        "duplicate_event_fingerprints": duplicate_event_fingerprints,
        "conversation_opens": threads_opened,
        "conversation_clicks": int(
            filter_scope.get("conversation_buttons_clicked", 0) or 0
        ),
        "ownership_classifications": ownership_counts,
        "routing_actions": routing_action_counts,
        "automatic_thread_opens": 0,
        "appfolio_writes_attempted": 0,
        "outbound_messages_attempted": 0,
    }
    assert_zero_write_metrics(
        poll_metrics, context="communication_inbox_poll"
    )
    if poll_metrics["conversation_opens"] or poll_metrics[
        "automatic_thread_opens"
    ]:
        raise RuntimeError("communication_inbox_poll_opened_thread")
    if metrics is not None:
        metrics.clear()
        metrics.update(poll_metrics)
    return stored_count


def _mark_communication_routing_status(
    communication_id: str, status: str, db_path: str
) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE communications SET processing_status = ?, "
            "last_updated_at = ? WHERE communication_id = ?",
            (status, datetime.now().isoformat(), communication_id),
        )
        conn.commit()
    finally:
        conn.close()


def process_pending_tenant_triage_replies(*, db_path=None) -> int:
    """Deliver Rachel's real continuation for a tenant's AppFolio-texted

    triage reply. `check_appfolio_inbox` above only observes and stores
    these (it is contractually never allowed to open a thread or write);
    this is the one place that acts on what it found, and it reuses the
    exact same `handle_tenant_triage_reply` already proven live over the
    email-triage path, so texting-based and email-based tenant replies
    are handled by one identical, already-tested function.
    """
    read_db_path = db_path or DB_PATH
    conn = sqlite3.connect(read_db_path)
    conn.row_factory = sqlite3.Row
    try:
        candidates = conn.execute(
            "SELECT communication_id, related_wo, message_text "
            "FROM communications WHERE direction = 'inbound' "
            "AND processing_status = 'list_observed_unopened' "
            "AND related_wo != '' AND contact_role IN ('tenant', 'resident') "
            "ORDER BY occurred_at ASC"
        ).fetchall()
    finally:
        conn.close()

    from rachel_wo_triage import handle_tenant_triage_reply

    processed = 0
    for row in candidates:
        wo_num = str(row["related_wo"])
        message = str(row["message_text"] or "").strip()
        communication_id = str(row["communication_id"])
        conn = sqlite3.connect(read_db_path)
        try:
            status_row = conn.execute(
                "SELECT status FROM wo_triage WHERE wo_num = ?", (wo_num,)
            ).fetchone()
        finally:
            conn.close()
        current_status = str(status_row[0]) if status_row else ""
        if current_status != "awaiting_reply":
            _mark_communication_routing_status(
                communication_id,
                f"skipped_status_{current_status or 'missing'}",
                read_db_path,
            )
            continue
        if not message:
            _mark_communication_routing_status(
                communication_id, "skipped_empty_message", read_db_path
            )
            continue
        try:
            handled = bool(
                handle_tenant_triage_reply(wo_num, message, channel="text")
            )
        except Exception as error:
            print(
                f"[AF-PendingTriage] WO#{wo_num} routing failed: "
                f"{type(error).__name__}"
            )
            _mark_communication_routing_status(
                communication_id, "failed", read_db_path
            )
            continue
        _mark_communication_routing_status(
            communication_id,
            "delivered" if handled else "handler_returned_false",
            read_db_path,
        )
        if handled:
            processed += 1
    print(
        f"[AF-PendingTriage] processed={processed} "
        f"candidates={len(candidates)}"
    )
    return processed


def process_rachel_owned_appfolio_threads(
    page, *, db_path=None, metrics=None
) -> int:
    """Read every changed Rachel-owned row; never open PM/unknown threads."""
    started = time.monotonic()
    read_db_path = db_path or DB_PATH
    initialize_event_memory(read_db_path)
    scope = ensure_appfolio_inbox_full_monitoring_scope(page)
    if not scope.get("verified_full_scope"):
        raise RuntimeError("Rachel-owned thread processing requires full scope")
    rows = _read_lightweight_inbox_rows(page)
    classifications = [
        (row, classify_communication_ownership(row, db_path=read_db_path))
        for row in rows
    ]
    eligible = [
        row for row, ownership in classifications
        if ownership.get("automatic_open_allowed")
    ]
    conn = sqlite3.connect(read_db_path)
    conn.row_factory = sqlite3.Row
    try:
        identity_states = {
            str(row["conversation_identity_key"] or ""): dict(row)
            for row in conn.execute(
                "SELECT conversation_identity_key, latest_state_key, "
                "last_unread, processing_status "
                "FROM appfolio_thread_selector_state "
                "WHERE conversation_identity_key != ''"
            ).fetchall()
        }
    finally:
        conn.close()
    targeted = []
    target_reasons = {}
    for row in eligible:
        identity_key = str(
            row.get("conversation_identity_key", "")
            or conversation_identity_key(row)
        )
        state_key = str(
            row.get("conversation_state_key", "")
            or conversation_state_key(row)
        )
        state = identity_states.get(identity_key)
        reason = ""
        if not state:
            reason = "unknown_conversation"
        elif state.get("processing_status") != "thread_read_verified":
            reason = "prior_read_unverified"
        elif str(state.get("latest_state_key") or "") == state_key:
            reason = ""
        elif not str(row.get("source_message_id", "") or "").startswith(
            "list-event-"
        ):
            # A reliable, AppFolio-native message id actually changed - real
            # new content, independent of read/unread state.
            reason = "conversation_state_changed"
        elif bool(row.get("unread")):
            # No native message id to lean on, but the row is currently
            # unread - treat as new activity. A mere read-state/timestamp
            # rerender of an already-verified, now-read thread must not
            # trigger reprocessing on its own.
            reason = "conversation_state_changed"
        if reason:
            targeted.append((row, reason))
            target_reasons[reason] = target_reasons.get(reason, 0) + 1
    processed = 0
    failures = 0
    for row, target_reason in targeted:
        identity_key = str(
            row.get("conversation_identity_key", "")
            or conversation_identity_key(row)
        )
        state_key = str(
            row.get("conversation_state_key", "")
            or conversation_state_key(row)
        )
        selector_key = str(row.get("selector_key", "") or "")
        event_key = str(row.get("event_key", "") or inbox_event_key(row))
        now = datetime.now().isoformat()
        conn = sqlite3.connect(read_db_path)
        try:
            conn.execute(
                """
                INSERT INTO appfolio_thread_selector_state (
                    selector_key, thread_id, latest_event_key, last_unread,
                    processing_status, last_error, updated_at,
                    conversation_identity_key, latest_state_key
                ) VALUES (?, ?, ?, ?, 'processing', '', ?, ?, ?)
                ON CONFLICT(conversation_identity_key)
                WHERE conversation_identity_key != '' DO UPDATE SET
                    selector_key = excluded.selector_key,
                    thread_id = excluded.thread_id,
                    processing_status = 'processing', last_error = '',
                    updated_at = excluded.updated_at
                """,
                (
                    selector_key, str(row.get("thread_id", "") or ""),
                    event_key, int(bool(row.get("unread"))), now,
                    identity_key, state_key,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        try:
            read_result = inspect_one_appfolio_inbox_thread_read_only(
                page,
                target_selector_key=selector_key,
                expected_event_key="",
                authorization=None,
                db_path=read_db_path,
            )
            status = "thread_read_verified"
            error_reason = ""
            last_unread = int(bool(
                read_result.get("unread_after", row.get("unread"))
            ))
            processed += 1
        except Exception as error:
            status = "failed"
            error_reason = type(error).__name__
            last_unread = int(bool(row.get("unread")))
            failures += 1
        conn = sqlite3.connect(read_db_path)
        try:
            conn.execute(
                "UPDATE appfolio_thread_selector_state SET "
                "latest_event_key = ?, latest_state_key = ?, "
                "last_unread = ?, processing_status = ?, last_error = ?, "
                "updated_at = ? WHERE conversation_identity_key = ?",
                (
                    event_key, state_key, last_unread, status, error_reason,
                    datetime.now().isoformat(), identity_key,
                ),
            )
            conn.commit()
        finally:
            conn.close()
    result = {
        "rows_seen": len(rows),
        "rachel_owned_rows": len(eligible),
        "rachel_owned_unchanged_skipped": len(eligible) - len(targeted),
        "targeted_thread_opens": len(targeted),
        "target_reasons": target_reasons,
        "threads_read_verified": processed,
        "thread_read_failures": failures,
        "human_or_unknown_thread_opens": 0,
        "appfolio_writes_attempted": 0,
        "outbound_messages_attempted": 0,
        "duration_seconds": round(time.monotonic() - started, 2),
    }
    assert_zero_write_metrics(
        result, context="rachel_owned_thread_reader"
    )
    print("[AF-OwnedThreadMetrics] " + json.dumps(result, sort_keys=True))
    if metrics is not None:
        metrics.clear()
        metrics.update(result)
    if failures:
        raise RuntimeError(
            "Rachel-owned thread processing failed: "
            f"thread_read_failures={failures}"
        )
    return processed


def _default_response_draft_generator(prompt: str) -> str:
    """Generate draft text only; this adapter never touches AppFolio."""
    response = client.messages.create(
        model=os.getenv(
            "RACHEL_COMMUNICATION_DRAFT_MODEL", "claude-sonnet-4-6"
        ),
        max_tokens=int(os.getenv(
            "RACHEL_COMMUNICATION_DRAFT_MAX_TOKENS", "600"
        )),
        system=(
            "You are Rachel, an assistant property manager for ClientCo "
            "Property Management. Draft only. Do not claim an action was "
            "completed, promise approval, assign a vendor, set an appointment, "
            "or invent facts. Appointments must be two-hour arrival windows."
        ),
        messages=[{"role": "user", "content": prompt}],
    )
    blocks = list(getattr(response, "content", []) or [])
    return " ".join(
        str(getattr(block, "text", "") or "").strip()
        for block in blocks
        if str(getattr(block, "text", "") or "").strip()
    )


def process_pending_rachel_owned_response_drafts(
    *, db_path=None, generator=None, metrics=None
) -> int:
    """Draft from durable verified history without reopening AppFolio.

    A failed model call leaves no draft row, so the next poll retries from
    memory. Existing idempotent drafts skip the model call. No composer or
    browser object is accepted by this function.
    """
    started = time.monotonic()
    draft_db_path = db_path or DB_PATH
    initialize_event_memory(draft_db_path)
    conn = sqlite3.connect(draft_db_path)
    try:
        # Group by the durable conversation_identity_key when available so a
        # conversation whose legacy thread id changed (a native AppFolio
        # thread id becoming available where only a synthetic fallback
        # existed before) is one candidate, not two. Rows never backfilled
        # with an identity key (empty string) fall back to grouping by the
        # legacy thread id alone, so nothing is silently dropped.
        thread_ids = [
            (str(row[0] or ""), str(row[1] or ""))
            for row in conn.execute(
                """
                SELECT MIN(source_thread_id) AS thread_id,
                       MAX(conversation_identity_key) AS identity_key
                FROM communications
                WHERE source = ? AND internal_user_id = ?
                AND source_thread_id != ''
                GROUP BY COALESCE(
                    NULLIF(conversation_identity_key, ''), source_thread_id
                )
                ORDER BY MAX(last_updated_at), thread_id
                """,
                ("appfolio_communication_inbox_thread", "rachel-asst"),
            ).fetchall()
        ]
    finally:
        conn.close()

    result = {
        "candidate_threads": len(thread_ids),
        "drafts_created": 0,
        "existing_drafts_skipped": 0,
        "non_actionable_threads_skipped": 0,
        "draft_failures": 0,
        "draft_failure_reasons": {},
        "composer_actions_attempted": 0,
        "send_actions_attempted": 0,
        "appfolio_writes_attempted": 0,
    }
    draft_generator = generator or _default_response_draft_generator
    for thread_id, identity_key in thread_ids:
        try:
            plan = create_response_draft_plan(
                thread_id,
                ownership={"classification": "rachel_owned"},
                authorization_reason="thread_owned_by_rachel",
                db_path=draft_db_path,
                conversation_identity_key=identity_key,
            )
        except CommunicationWorkflowError as error:
            if str(error) == "no_unanswered_inbound_message":
                result["non_actionable_threads_skipped"] += 1
                continue
            result["draft_failures"] += 1
            reason = str(error)
            result["draft_failure_reasons"][reason] = (
                result["draft_failure_reasons"].get(reason, 0) + 1
            )
            continue

        conn = sqlite3.connect(draft_db_path)
        try:
            existing = conn.execute(
                "SELECT 1 FROM communication_drafts "
                "WHERE idempotency_key = ? LIMIT 1",
                (plan["idempotency_key"],),
            ).fetchone()
        finally:
            conn.close()
        if existing:
            result["existing_drafts_skipped"] += 1
            continue

        try:
            draft_text = generate_response_draft(
                plan, generator=draft_generator
            )
            stored = store_response_draft(
                plan, draft_text, db_path=draft_db_path
            )
            if stored.get("created"):
                result["drafts_created"] += 1
            else:
                result["existing_drafts_skipped"] += 1
        except Exception as error:
            result["draft_failures"] += 1
            reason = f"draft_generation_{type(error).__name__}"
            result["draft_failure_reasons"][reason] = (
                result["draft_failure_reasons"].get(reason, 0) + 1
            )

    result["duration_seconds"] = round(time.monotonic() - started, 2)
    print("[AF-DraftMetrics] " + json.dumps(result, sort_keys=True))
    if metrics is not None:
        metrics.clear()
        metrics.update(result)
    if result["draft_failures"]:
        raise RuntimeError(
            "Rachel-owned response drafting failed: "
            f"draft_failures={result['draft_failures']}"
        )
    return result["drafts_created"]


def process_triage_reply(page, wo_row: dict, tenant_reply: str) -> bool:
    """
    Continue the triage conversation with a tenant who replied via text.
    Uses the SAME page to send the next message back via the reply field.
    """
    raise RuntimeError(
        "Legacy inbox triage reply path is disabled; use the verified "
        "thread workflow."
    )
    wo_num = wo_row["wo_num"]
    wo_url = wo_row["wo_url"]
    tenant = wo_row["tenant"]
    address = wo_row["address"]
    job_desc = wo_row["job_desc"]
    issue_type = wo_row["issue_type"]
    conversation = json.loads(wo_row["conversation"]) if wo_row["conversation"] else []

    # Add reply to conversation
    last_q = conversation[-1]["q"] if conversation else ""
    conversation.append({"q": last_q, "a": tenant_reply})

    # Ask Claude if we have enough info
    if len(conversation) >= 4:
        ready = True
        _, summary = has_enough_info(issue_type, conversation)
        next_action = summary
    else:
        ready, next_action = has_enough_info(issue_type, conversation)
        summary = next_action

    # Tenant wants to speak to a supervisor
    if ready == "escalate":
        escalate_msg = (
            "Absolutely — I'm going to have Regional Manager, our Regional "
            "Property Manager, reach out to you directly. She'll be in "
            "touch shortly.")
        send_reply_on_page(page, escalate_msg)
        send_sms_via_gmail(
            f"⚠️ WO #{wo_num} — TENANT REQUESTING SUPERVISOR\n"
            f"{tenant} | {address}\n"
            f"Reason: {next_action}\n\n"
            f"Please reach out to the tenant directly:\n"
            f"Tenant reply: {_truncate_at_word_boundary(tenant_reply, 150)}",
            urgent=True)
        _save_triage_state(wo_num, wo_url, tenant, address,
                           job_desc, issue_type, conversation)
        conn = get_db()
        conn.execute(
            "UPDATE wo_triage SET status = 'escalated_to_manager' "
            "WHERE wo_num = ?", (wo_num,))
        conn.commit()
        conn.close()
        print(f"[AF-Inbox] WO#{wo_num} escalated to Manager")
        return True

    # Tenant fixed it via troubleshooting
    if ready == "resolved":
        resolved_msg = (
            "That's great news — glad we could get it sorted without a "
            "visit! If it acts up again, just reply here and we'll send "
            "someone out. Have a great day!")
        send_reply_on_page(page, resolved_msg)
        _notify_manager_exchange(wo_num, tenant, address,
                                tenant_reply, resolved_msg)
        send_sms_via_gmail(
            f"WO #{wo_num} — RESOLVED via troubleshooting, no dispatch "
            f"needed ✅\n{tenant} | {address}\nFix: {next_action}\n"
            f"Please mark the work order completed in AppFolio.",
            urgent=False)
        _save_triage_state(wo_num, wo_url, tenant, address,
                           job_desc, issue_type, conversation)
        conn = get_db()
        conn.execute(
            "UPDATE wo_triage SET status = 'resolved_troubleshooting' "
            "WHERE wo_num = ?", (wo_num,))
        conn.commit()
        conn.close()
        print(f"[AF-Inbox] WO#{wo_num} resolved via troubleshooting")
        return True

    if ready:
        # Send closing message to tenant
        send_reply_on_page(page, CLOSING_MESSAGE)
        _notify_manager_exchange(wo_num, tenant, address,
                                tenant_reply, CLOSING_MESSAGE)

        # Load stored issues for multi-issue summary
        stored_issues = []
        try:
            if wo_row.get("issues"):
                stored_issues = json.loads(wo_row["issues"])
        except Exception:
            pass

        if len(stored_issues) > 1:
            base_num = wo_num.split("-")[0]
            lines = [f"WO #{base_num} — {tenant}", f"{address}",
                     f"{len(stored_issues)} issues", ""]
            for idx, iss in enumerate(stored_issues, 1):
                sub = f"{base_num}-{idx}"
                vends = get_suggested_vendors(
                    address, iss.get("trade", "general")
                )[:1]
                lines.append(
                    f"{sub} {iss.get('trade','general').upper()}: "
                    f"{iss['label']}")
                for vi, v in enumerate(vends, 1):
                    ph = f" {v['phone']}" if v.get("phone") else ""
                    lines.append(f"  {vi}. {v['name']}{ph}")
                lines.append(f"  {len(vends)+1}. In-house")
                lines.append("")
            lines.append(f"Summary: {summary}")
            lines.append("")
            lines.append(
                f'Reply per line: "{base_num}-1: 1, {base_num}-2: 2, ..."')
            summary_msg = "\n".join(lines)
        else:
            vendors = get_suggested_vendors(address, issue_type)[:1]
            summary_msg = format_vendor_sms(
                wo_num, tenant, address, summary, vendors)

        send_sms_via_gmail(summary_msg, urgent=False)
        _save_triage_state(wo_num, wo_url, tenant, address,
                           job_desc, issue_type, conversation)
        conn = get_db()
        conn.execute(
            "UPDATE wo_triage SET status = 'complete' WHERE wo_num = ?",
            (wo_num,))
        conn.commit()
        conn.close()
        print(f"[AF-Inbox] ✅ WO#{wo_num} summary sent to Manager + Owner")
        return True
    else:
        # Ask the next question — reply directly on this page
        send_reply_on_page(page, next_action)
        conversation[-1]["q"] = next_action
        _save_triage_state(wo_num, wo_url, tenant, address,
                           job_desc, issue_type, conversation)
        print(f"[AF-Inbox] Next question sent: {next_action[:60]}")

        # Email Manager the exchange so she can see the conversation
        _notify_manager_exchange(wo_num, tenant, address,
                                tenant_reply, next_action)
        return True


def send_reply_on_page(page, message: str) -> bool:
    """
    Send a reply using the SMS reply field at the bottom of the
    AppFolio texting thread page.
    """
    raise RuntimeError(
        "Legacy broad-selector reply sender is disabled; use a claimed "
        "exact Communication send intent."
    )
    try:
        # Find the reply input — "Enter SMS message or type / to use a template"
        reply_input = page.query_selector(
            'textarea[placeholder*="SMS message"], '
            'textarea[placeholder*="Enter SMS"], '
            'textarea[placeholder*="message"]')
        if not reply_input or not reply_input.is_visible():
            # Try broader search
            textareas = page.query_selector_all('textarea')
            for ta in textareas:
                if ta.is_visible():
                    reply_input = ta
                    break

        if reply_input and reply_input.is_visible():
            reply_input.click()
            human_delay(0.5, 1)
            reply_input.fill(message)
            human_delay(1, 2)

            # Click Send button
            send_btn = page.query_selector(
                'button:has-text("Send")')
            if send_btn and send_btn.is_visible():
                send_btn.click()
                human_delay(2, 3)
                print(f"[AF-Inbox] ✅ Reply sent via AppFolio texting")
                return True
            else:
                print("[AF-Inbox] Send button not found")
        else:
            print("[AF-Inbox] Reply textarea not found")
    except Exception as e:
        print(f"[AF-Inbox] Reply failed: {e}")
    return False


def _notify_manager_exchange(wo_num, tenant, address,
                            tenant_said, rachel_said):
    """Email Manager + Owner a copy of each triage exchange."""
    send_sms_via_gmail(
        f"WO #{wo_num} — Triage update\n"
        f"{tenant} | {address}\n\n"
        f"Tenant: {_truncate_at_word_boundary(tenant_said, 200)}\n\n"
        f"Rachel: {_truncate_at_word_boundary(rachel_said, 200)}",
        urgent=False)


if __name__ == "__main__":
    from rachel_appfolio_browser import get_appfolio_browser_manager

    manager = get_appfolio_browser_manager()
    with manager.operation("manual_appfolio_inbox_poll") as page:
        check_appfolio_inbox(page)
