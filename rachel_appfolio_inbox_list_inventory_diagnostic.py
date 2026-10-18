"""Focused, read-only AppFolio Communication Inbox list inventory."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from rachel_appfolio_browser import (
    get_appfolio_browser_manager,
    get_downloads_dir,
)
from rachel_appfolio_inbox import INBOX_URL


TRUE_VALUES = {"1", "true", "yes", "on"}


def _enabled(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in TRUE_VALUES


_INBOX_LIST_INVENTORY_SCRIPT = r"""
    async () => {
        const round = value => Math.round(Number(value || 0) * 10) / 10;
        const viewport = {
            width: window.innerWidth,
            height: window.innerHeight
        };
        const allElements = [];
        const collect = element => {
            allElements.push(element);
            for (const child of Array.from(element.children || [])) collect(child);
            if (element.shadowRoot) {
                for (const child of Array.from(element.shadowRoot.children || [])) {
                    collect(child);
                }
            }
        };
        if (document.documentElement) collect(document.documentElement);

        const composedParent = element => {
            if (!element) return null;
            if (element.parentElement) return element.parentElement;
            const root = element.getRootNode ? element.getRootNode() : null;
            return root && root.host instanceof Element ? root.host : null;
        };
        const rectFor = element => {
            const rect = element.getBoundingClientRect();
            return {
                x: round(rect.x),
                y: round(rect.y),
                width: round(rect.width),
                height: round(rect.height),
                right: round(rect.right),
                bottom: round(rect.bottom)
            };
        };
        const rendered = element => {
            const rect = element.getBoundingClientRect();
            const style = window.getComputedStyle(element);
            return rect.width > 0 && rect.height > 0 &&
                style.display !== 'none' && style.visibility !== 'hidden' &&
                style.opacity !== '0';
        };
        const inViewport = element => {
            if (!rendered(element)) return false;
            const rect = element.getBoundingClientRect();
            return rect.bottom > 0 && rect.right > 0 &&
                rect.top < viewport.height && rect.left < viewport.width;
        };
        const viewportRelation = element => {
            if (!rendered(element)) return 'hidden';
            const rect = element.getBoundingClientRect();
            if (rect.top >= viewport.height) return 'below_viewport';
            if (rect.bottom <= 0) return 'above_viewport';
            if (rect.left >= viewport.width) return 'right_of_viewport';
            if (rect.right <= 0) return 'left_of_viewport';
            if (rect.top < 0 || rect.bottom > viewport.height ||
                rect.left < 0 || rect.right > viewport.width) {
                return 'partially_in_viewport';
            }
            return 'in_viewport';
        };

        const classFrequency = new Map();
        for (const element of allElements) {
            for (const token of Array.from(element.classList || [])) {
                classFrequency.set(token, (classFrequency.get(token) || 0) + 1);
            }
        }
        const safeClassTokens = element => Array.from(element.classList || [])
            .filter(token => {
                if (/@|\+|\d{7,}/.test(token) || token.length > 100) return false;
                return (classFrequency.get(token) || 0) >= 2;
            })
            .slice(0, 20);
        const classSemanticHints = element => {
            const result = new Set();
            const categories = [
                'absolute', 'active', 'badge', 'card', 'conversation', 'date',
                'dot', 'fixed', 'hidden', 'infinite', 'item', 'list', 'message',
                'preview', 'read', 'recycle', 'row', 'scroll', 'selected',
                'snippet', 'status', 'time', 'transform', 'unread', 'virtual'
            ];
            for (const token of Array.from(element.classList || [])) {
                const lower = token.toLowerCase();
                for (const category of categories) {
                    if (lower.includes(category)) result.add(category);
                }
            }
            return Array.from(result).sort();
        };
        const standardRoles = new Set([
            'button', 'grid', 'gridcell', 'group', 'link', 'list', 'listbox',
            'listitem', 'main', 'menu', 'menuitem', 'navigation', 'option',
            'region', 'row', 'rowgroup', 'search', 'table', 'tab', 'tabpanel',
            'toolbar', 'tree', 'treeitem'
        ]);
        const safeRole = element => {
            const role = String(element.getAttribute('role') || '').toLowerCase();
            if (!role) return '';
            return standardRoles.has(role) ? role : '<NONSTANDARD_ROLE>';
        };
        const safeAttributeNames = element => Array.from(element.attributes || [])
            .map(attribute => attribute.name.toLowerCase())
            .filter(name => name !== 'class' && name !== 'style')
            .filter(name => !/@|\+|\d{7,}/.test(name) && name.length <= 100)
            .sort()
            .slice(0, 30);
        const signatureDescriptor = element => ({
            tag_name: element.tagName.toLowerCase(),
            reusable_class_tokens: safeClassTokens(element),
            role: safeRole(element),
            structural_attribute_names: safeAttributeNames(element),
            direct_child_tags: Array.from(element.children || [])
                .map(child => child.tagName.toLowerCase())
                .slice(0, 30)
        });
        const signatureTokens = new Map();
        const signatureFor = element => {
            if (!element) return '';
            const key = JSON.stringify(signatureDescriptor(element));
            if (!signatureTokens.has(key)) {
                signatureTokens.set(key, `SIGNATURE_${signatureTokens.size + 1}`);
            }
            return signatureTokens.get(key);
        };
        const nodeTokens = new WeakMap();
        let nextNodeToken = 1;
        const nodeToken = element => {
            if (!nodeTokens.has(element)) {
                nodeTokens.set(element, `NODE_${nextNodeToken}`);
                nextNodeToken += 1;
            }
            return nodeTokens.get(element);
        };
        const textShape = element => {
            const slots = [];
            for (const candidate of [element, ...Array.from(element.querySelectorAll('*'))]) {
                const count = Array.from(candidate.childNodes || [])
                    .filter(node => node.nodeType === Node.TEXT_NODE)
                    .reduce(
                        (total, node) => total +
                            String(node.nodeValue || '').trim().length,
                        0
                    );
                if (count > 0) slots.push(count);
                if (slots.length >= 20) break;
            }
            return slots;
        };
        const summarize = element => {
            const parent = composedParent(element);
            const grandparent = composedParent(parent);
            const style = window.getComputedStyle(element);
            return {
                node_token: nodeToken(element),
                structural_signature_token: signatureFor(element),
                signature: signatureDescriptor(element),
                parent_structural_signature_token: signatureFor(parent),
                grandparent_structural_signature_token: signatureFor(grandparent),
                geometry: rectFor(element),
                rendered: rendered(element),
                in_viewport: inViewport(element),
                viewport_relation: viewportRelation(element),
                direct_child_count: element.children.length,
                descendant_element_count: element.querySelectorAll('*').length,
                position: style.position,
                overflow_y: style.overflowY,
                scroll_height: element.scrollHeight,
                client_height: element.clientHeight,
                scroll_top: round(element.scrollTop),
                transform_present: style.transform !== 'none',
                transform_kind: style.transform === 'none'
                    ? 'none' : '<TRANSFORM_PRESENT>',
                translate_present: style.translate && style.translate !== 'none',
                content_visibility: style.contentVisibility || '',
                contain: style.contain || '',
                will_change_transform: String(style.willChange || '')
                    .split(',').map(item => item.trim()).includes('transform'),
                class_semantic_hints: classSemanticHints(element),
                text_slot_character_counts: textShape(element)
            };
        };
        const directButtonRows = list => Array.from(list.children || [])
            .filter(child => child.matches('button'))
            .filter(button => Array.from(button.children || [])
                .some(child => child.matches('li.list-group-item')));
        const listGroups = allElements.filter(element =>
            element.matches('ul.list-group')
        );
        const listItems = allElements.filter(element =>
            element.matches('li.list-group-item')
        );
        const buttons = allElements.filter(element =>
            element.matches('button')
        );
        const buttonsWithListItem = buttons.filter(button =>
            Array.from(button.children || []).some(child =>
                child.matches('li.list-group-item')
            )
        );
        const ulInventory = listGroups.map(list => ({
            ...summarize(list),
            direct_li_count: Array.from(list.children || [])
                .filter(child => child.matches('li.list-group-item')).length,
            direct_button_count: Array.from(list.children || [])
                .filter(child => child.matches('button')).length,
            direct_button_with_li_count: directButtonRows(list).length,
            descendant_li_count: list.querySelectorAll('li.list-group-item').length
        }));
        const liInventory = listItems.map(item => ({
            ...summarize(item),
            direct_button_parent: Boolean(
                item.parentElement && item.parentElement.matches('button')
            ),
            list_group_ancestor: Boolean(item.closest('ul.list-group'))
        }));
        const buttonInventory = buttons.map(button => ({
            ...summarize(button),
            contains_direct_list_item: Array.from(button.children || [])
                .some(child => child.matches('li.list-group-item')),
            descendant_list_item_count:
                button.querySelectorAll('li.list-group-item').length
        }));

        const scrollableElements = allElements.filter(element => {
            const style = window.getComputedStyle(element);
            return ['auto', 'scroll', 'overlay'].includes(style.overflowY) ||
                element.scrollHeight > element.clientHeight + 1;
        });
        const scrollableInventory = scrollableElements.map(element => ({
            ...summarize(element),
            descendant_ul_list_group_count:
                element.querySelectorAll('ul.list-group').length,
            descendant_li_list_group_item_count:
                element.querySelectorAll('li.list-group-item').length,
            descendant_button_with_li_count: Array.from(
                element.querySelectorAll('button')
            ).filter(button => Array.from(button.children || []).some(child =>
                child.matches('li.list-group-item')
            )).length
        }));
        const belowViewport = allElements.filter(element =>
            rendered(element) && element.getBoundingClientRect().top >= viewport.height
        );
        const positioningIndicators = allElements.filter(element => {
            const style = window.getComputedStyle(element);
            return ['absolute', 'fixed', 'sticky'].includes(style.position) ||
                style.transform !== 'none' ||
                (style.translate && style.translate !== 'none') ||
                String(style.willChange || '').includes('transform') ||
                String(style.contentVisibility || '') === 'auto' ||
                /layout|paint|strict|content/.test(String(style.contain || '')) ||
                classSemanticHints(element).some(hint =>
                    ['infinite', 'recycle', 'transform', 'virtual'].includes(hint)
                );
        });

        const cardGroups = new Map();
        for (const button of buttonsWithListItem) {
            const token = signatureFor(button);
            if (!cardGroups.has(token)) cardGroups.set(token, []);
            cardGroups.get(token).push(button);
        }
        const repeatedCardStructures = Array.from(cardGroups.entries())
            .map(([token, cards]) => ({
                structural_signature_token: token,
                signature: signatureDescriptor(cards[0]),
                count: cards.length,
                cards: cards.map(summarize)
            }))
            .sort((left, right) => right.count - left.count);

        const pseudoState = element => {
            if (!element) return null;
            const describePseudo = pseudo => {
                const style = window.getComputedStyle(element, pseudo);
                const generated = String(style.content || '');
                return {
                    generated_marker_present: ![
                        '', 'none', 'normal', '\"\"'
                    ].includes(generated),
                    background_color: style.backgroundColor,
                    foreground_color: style.color,
                    width: style.width,
                    height: style.height,
                    display: style.display,
                    border_radius: style.borderRadius
                };
            };
            return {
                before: describePseudo('::before'),
                after: describePseudo('::after')
            };
        };
        const fontWeight = element => {
            if (!element) return 0;
            const value = window.getComputedStyle(element).fontWeight;
            return value === 'bold' ? 700 : (Number.parseInt(value, 10) || 0);
        };
        const unreadInventory = buttonsWithListItem.map(button => {
            const item = Array.from(button.children || []).find(child =>
                child.matches('li.list-group-item')
            );
            const layout = item && Array.from(item.children || []).find(child =>
                child.matches('div.d-flex')
            );
            const indicator = layout && Array.from(layout.children || []).find(child =>
                child.matches('div.d-flex.align-items-center')
            );
            const contact = item && item.querySelector('span[data-testid]');
            const preview = item && item.querySelector('span.d-block.text-truncate');
            const style = item ? window.getComputedStyle(item) : null;
            const classHints = new Set([
                ...classSemanticHints(button),
                ...(item ? classSemanticHints(item) : []),
                ...(indicator ? classSemanticHints(indicator) : [])
            ]);
            return {
                card_node_token: nodeToken(button),
                card_geometry: rectFor(button),
                unread_class_hint_present: Array.from(classHints).includes('unread'),
                read_class_hint_present: Array.from(classHints).includes('read'),
                badge_or_dot_hint_present: Array.from(classHints)
                    .some(hint => ['badge', 'dot', 'status'].includes(hint)),
                aria_label_contains_unread: [button, item].filter(Boolean)
                    .some(element => /\bunread\b/i.test(
                        element.getAttribute('aria-label') || ''
                    )),
                contact_font_weight: fontWeight(contact),
                preview_font_weight: fontWeight(preview),
                item_background_color: style ? style.backgroundColor : '',
                item_foreground_color: style ? style.color : '',
                indicator: indicator ? {
                    ...summarize(indicator),
                    visible_descendant_count: Array.from(
                        indicator.querySelectorAll('*')
                    ).filter(rendered).length,
                    direct_text_character_count: Array.from(
                        indicator.childNodes || []
                    ).filter(node => node.nodeType === Node.TEXT_NODE)
                        .reduce((total, node) => total +
                            String(node.nodeValue || '').trim().length, 0),
                    pseudo_state: pseudoState(indicator)
                } : null
            };
        });

        const scrollSnapshots = [];
        const anchorLists = listGroups.filter(list => directButtonRows(list).length > 0);
        const scrollAncestor = list => {
            let current = composedParent(list);
            while (current && current !== document.body &&
                current !== document.documentElement) {
                const style = window.getComputedStyle(current);
                if (['auto', 'scroll', 'overlay'].includes(style.overflowY) &&
                    current.scrollHeight > current.clientHeight + 1) {
                    return current;
                }
                current = composedParent(current);
            }
            return null;
        };
        const pane = anchorLists.map(scrollAncestor).find(Boolean) || null;
        const originalPaneScrollTop = pane ? pane.scrollTop : 0;
        let scrollPositionRestored = true;
        const settle = () => new Promise(resolve => {
            window.requestAnimationFrame(() =>
                window.requestAnimationFrame(() => window.setTimeout(resolve, 250))
            );
        });
        const snapshotAt = label => {
            const currentListGroups = Array.from(document.querySelectorAll('ul.list-group'));
            const currentItems = Array.from(
                document.querySelectorAll('li.list-group-item')
            );
            const currentCards = Array.from(document.querySelectorAll('button'))
                .filter(button => Array.from(button.children || []).some(child =>
                    child.matches('li.list-group-item')
                ));
            return {
                label: label,
                pane_scroll_top: pane ? round(pane.scrollTop) : 0,
                ul_list_group_count: currentListGroups.length,
                li_list_group_item_count: currentItems.length,
                rendered_li_count: currentItems.filter(rendered).length,
                in_viewport_li_count: currentItems.filter(inViewport).length,
                below_viewport_li_count: currentItems.filter(item =>
                    rendered(item) && item.getBoundingClientRect().top >= viewport.height
                ).length,
                button_count: document.querySelectorAll('button').length,
                button_with_direct_li_count: currentCards.length,
                candidate_cards: currentCards.map(card => ({
                    node_token: nodeToken(card),
                    structural_signature_token: signatureFor(card),
                    geometry: rectFor(card),
                    viewport_relation: viewportRelation(card),
                    text_slot_character_counts: textShape(card)
                }))
            };
        };
        if (pane) {
            const originalScrollTop = pane.scrollTop;
            const maximumScrollTop = Math.max(0, pane.scrollHeight - pane.clientHeight);
            const positions = Array.from(new Set([
                originalScrollTop,
                0,
                maximumScrollTop * 0.25,
                maximumScrollTop * 0.5,
                maximumScrollTop * 0.75,
                maximumScrollTop
            ].map(value => Math.max(0, Math.min(maximumScrollTop, round(value))))));
            try {
                for (let index = 0; index < positions.length; index += 1) {
                    pane.scrollTop = positions[index];
                    await settle();
                    scrollSnapshots.push(snapshotAt(`POSITION_${index + 1}`));
                }
            } finally {
                pane.scrollTop = originalScrollTop;
                await settle();
                scrollPositionRestored =
                    round(pane.scrollTop) === round(originalScrollTop);
            }
        } else {
            scrollSnapshots.push(snapshotAt('NO_SCROLLABLE_LIST_PANE'));
        }

        const maxCardsAcrossScroll = Math.max(
            0, ...scrollSnapshots.map(snapshot => snapshot.button_with_direct_li_count)
        );
        const cardNodeTokens = new Set(scrollSnapshots.flatMap(snapshot =>
            snapshot.candidate_cards.map(card => card.node_token)
        ));
        const cardNodeShapeVariants = new Map();
        for (const snapshot of scrollSnapshots) {
            for (const card of snapshot.candidate_cards) {
                if (!cardNodeShapeVariants.has(card.node_token)) {
                    cardNodeShapeVariants.set(card.node_token, new Set());
                }
                cardNodeShapeVariants.get(card.node_token).add(
                    JSON.stringify(card.text_slot_character_counts)
                );
            }
        }
        const recycledCardNodeCount = Array.from(
            cardNodeShapeVariants.values()
        ).filter(shapes => shapes.size > 1).length;
        const otherListItems = liInventory.filter(item =>
            !item.direct_button_parent
        );
        const anchorRect = anchorLists.length
            ? anchorLists[0].getBoundingClientRect() : null;
        const horizontallyNearAnchor = item => {
            if (!anchorRect || !item.rendered) return false;
            const rect = item.geometry;
            const overlap = Math.max(
                0,
                Math.min(rect.right, anchorRect.right) -
                    Math.max(rect.x, anchorRect.left)
            );
            return overlap >= Math.min(rect.width, anchorRect.width) * 0.5;
        };
        const otherListItemsNearPane = otherListItems.filter(
            horizontallyNearAnchor
        );
        const otherListContainersNearPane = ulInventory.filter(item => {
            if (!anchorRect || !item.rendered ||
                item.direct_button_with_li_count > 0) return false;
            const rect = item.geometry;
            const overlap = Math.max(
                0,
                Math.min(rect.right, anchorRect.right) -
                    Math.max(rect.x, anchorRect.left)
            );
            return overlap >= Math.min(rect.width, anchorRect.width) * 0.5 &&
                item.descendant_li_count > 0;
        });
        const distinctScrollPositions = new Set(
            scrollSnapshots.map(snapshot => snapshot.pane_scroll_top)
        ).size;
        let conclusionCode = 'INDETERMINATE';
        if (buttonsWithListItem.length > 1) {
            conclusionCode = 'MULTIPLE_ROWS_EXIST_AND_SELECTOR_MATCHES';
        } else if (otherListItemsNearPane.length > 0) {
            conclusionCode = 'ADDITIONAL_ROWS_USE_DIFFERENT_PARENT_STRUCTURE';
        } else if (otherListContainersNearPane.length > 0) {
            conclusionCode = 'ANOTHER_CONTAINER_HOLDS_ADDITIONAL_ROWS';
        } else if (maxCardsAcrossScroll > buttonsWithListItem.length) {
            conclusionCode = recycledCardNodeCount > 0
                ? 'VIRTUALIZED_ROW_NODES_RECYCLED_WHILE_SCROLLING'
                : 'ADDITIONAL_ROWS_LAZY_RENDERED_WHILE_SCROLLING';
        } else if (pane && distinctScrollPositions > 1 &&
            maxCardsAcrossScroll <= 2 &&
            (recycledCardNodeCount > 0 ||
                cardNodeTokens.size > buttonsWithListItem.length)) {
            conclusionCode = 'ONE_OR_FEW_VIRTUALIZED_ROW_NODES';
        } else if (buttonsWithListItem.length === 1 &&
            maxCardsAcrossScroll === 1 &&
            otherListItemsNearPane.length === 0 &&
            otherListContainersNearPane.length === 0) {
            conclusionCode = 'TEST_SESSION_RENDERED_ONE_CONVERSATION_ROW';
        }
        return {
            schema_version: 1,
            sanitized: true,
            text_policy: '<NO_RAW_DOM_TEXT_RECORDED>',
            attribute_policy: '<ATTRIBUTE_NAMES_ONLY_NO_RAW_VALUES>',
            viewport: viewport,
            document_metrics: {
                scroll_height: document.documentElement.scrollHeight,
                client_height: document.documentElement.clientHeight,
                body_scroll_height: document.body ? document.body.scrollHeight : 0
            },
            counts: {
                all_elements: allElements.length,
                ul_list_group: listGroups.length,
                li_list_group_item: listItems.length,
                rendered_li_list_group_item: listItems.filter(rendered).length,
                hidden_li_list_group_item: listItems.filter(item => !rendered(item)).length,
                in_viewport_li_list_group_item: listItems.filter(inViewport).length,
                below_viewport_li_list_group_item: listItems.filter(item =>
                    rendered(item) && item.getBoundingClientRect().top >= viewport.height
                ).length,
                buttons: buttons.length,
                rendered_buttons: buttons.filter(rendered).length,
                buttons_containing_direct_li: buttonsWithListItem.length,
                scrollable_containers: scrollableElements.length,
                elements_below_viewport: belowViewport.length,
                positioning_or_virtualization_indicators:
                    positioningIndicators.length
            },
            ul_list_groups: ulInventory,
            li_list_group_items: liInventory,
            buttons: buttonInventory,
            buttons_containing_list_items: buttonsWithListItem.map(summarize),
            repeated_button_card_structures: repeatedCardStructures,
            scrollable_containers: scrollableInventory,
            elements_below_viewport: belowViewport.map(summarize),
            positioning_and_virtualization_indicators:
                positioningIndicators.map(summarize),
            unread_state_inventory: unreadInventory,
            list_pane_scroll_probe: {
                pane_found: Boolean(pane),
                pane: pane ? summarize(pane) : null,
                original_scroll_top: round(originalPaneScrollTop),
                final_scroll_top: pane ? round(pane.scrollTop) : 0,
                original_scroll_position_restored: scrollPositionRestored,
                snapshots: scrollSnapshots
            },
            hypothesis_evidence: {
                selector_matched_multiple_rows_initially:
                    buttonsWithListItem.length > 1,
                selector_found_more_rows_during_scroll:
                    maxCardsAcrossScroll > buttonsWithListItem.length,
                candidate_dom_nodes_seen_across_scroll: cardNodeTokens.size,
                candidate_dom_nodes_with_changed_text_shape:
                    recycledCardNodeCount,
                distinct_scroll_positions_sampled: distinctScrollPositions,
                candidate_count_stayed_small_across_scroll:
                    maxCardsAcrossScroll <= 2,
                other_list_items_exist: otherListItems.length > 0,
                other_rendered_list_items: otherListItems.filter(item =>
                    item.rendered
                ).length,
                other_list_items_near_conversation_pane:
                    otherListItemsNearPane.length,
                other_list_containers_near_conversation_pane:
                    otherListContainersNearPane.length,
                scrollable_list_pane_found: Boolean(pane),
                maximum_candidate_cards_at_any_position: maxCardsAcrossScroll,
                conclusion_code: conclusionCode
            },
            safety: {
                clicks_dispatched: 0,
                conversations_opened: 0,
                communications_modified: 0,
                scroll_position_restored: scrollPositionRestored
            }
        };
    }
"""


_FORBIDDEN_KEYS = {
    "text", "inner_text", "innertext", "text_content", "textcontent",
    "message", "message_text", "preview", "contact", "contact_name",
    "sender", "sender_name", "property", "property_address", "unit",
    "email", "phone", "attribute_value", "raw_value", "cookie", "token",
}


def _validate_sanitized(value) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if str(key).lower() in _FORBIDDEN_KEYS:
                raise ValueError(f"unsafe list inventory field returned: {key}")
            _validate_sanitized(nested)
    elif isinstance(value, list):
        for nested in value:
            _validate_sanitized(nested)


def main() -> int:
    if not _enabled("RACHEL_TEST_MODE"):
        raise RuntimeError("RACHEL_TEST_MODE must be enabled")
    if _enabled("RACHEL_APPFOLIO_QA_ENABLED"):
        raise RuntimeError("AppFolio write QA must be disabled")
    if _enabled("RACHEL_ENABLED"):
        raise RuntimeError("RACHEL_ENABLED must be false for this diagnostic")

    manager = get_appfolio_browser_manager()
    try:
        with manager.operation("read_only_inbox_list_inventory") as page:
            page.goto(INBOX_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(5000)
            report = page.evaluate(_INBOX_LIST_INVENTORY_SCRIPT) or {}
        if not isinstance(report, dict):
            raise TypeError("list inventory result was not an object")
        _validate_sanitized(report)
        report["browser_metrics"] = manager.metrics
        report["safety"]["read_only"] = True

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        output_dir = Path(get_downloads_dir())
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / (
            f"appfolio_inbox_list_inventory_sanitized_{timestamp}.json"
        )
        output_path.write_text(
            json.dumps(report, indent=2, sort_keys=True), encoding="utf-8"
        )

        counts = report.get("counts", {})
        probe = report.get("list_pane_scroll_probe", {})
        evidence = report.get("hypothesis_evidence", {})
        print(
            "[AF-InboxListInventory] Complete "
            f"ul={counts.get('ul_list_group', 0)} "
            f"li={counts.get('li_list_group_item', 0)} "
            f"visible_li={counts.get('in_viewport_li_list_group_item', 0)} "
            f"hidden_li={counts.get('hidden_li_list_group_item', 0)} "
            f"row_buttons={counts.get('buttons_containing_direct_li', 0)} "
            f"scroll_pane={int(bool(probe.get('pane_found')))} "
            "max_rows_across_scroll="
            f"{evidence.get('maximum_candidate_cards_at_any_position', 0)}"
        )
        print(f"[AF-InboxListInventory] Report: {output_path}")
        print(
            "[AF-InboxListInventory] No clicks or conversation opens; "
            "original scroll position restored."
        )
        return 0
    finally:
        manager.close()


if __name__ == "__main__":
    raise SystemExit(main())
