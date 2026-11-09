#!/usr/bin/env python3
# -*- coding: utf-8 -*-

#Copyright (c) 2026 Szymon Wolarz
#Licensed under the MIT License. See LICENSE file in the project root for full license information.

"""
MODULE: undo_manager.py
ROLE: Background Handler
DESCRIPTION:
Implements the change stack for Undo/Redo operations.
"""

class UndoManager:
    def __init__(self, main_window, canvas):
        from collections import deque
        self.main_window = main_window
        self.canvas = canvas
        self.max_size = 50
        self.undo_stack = deque(maxlen=self.max_size)
        self.redo_stack = deque(maxlen=self.max_size)

    def clear(self):
        """A new transcript was opened: nothing from the previous one can be undone into it."""
        self.undo_stack.clear()
        self.redo_stack.clear()

    def push(self, action):
        if not action or not action.get('changes'):
            return
        if getattr(self, '_applying', False):
            return  # side effects of an undo/redo (a refresh, speakers recounted) aren't new steps
        self.undo_stack.append(action)
        self.redo_stack.clear()
        
        if hasattr(self.main_window, 'autosave_manager'):
            self.main_window.autosave_manager.schedule(self.main_window._build_autosave_payload)

    def undo(self):
        if not self.undo_stack: return
        action = self.undo_stack.pop()
        self._applying = True
        try:
            redo_action = self._apply_action(action)
        finally:
            self._applying = False
        self.redo_stack.append(redo_action)
        self.canvas.update()
        if hasattr(self.main_window, 'waveform_timeline') and self.main_window.waveform_timeline:
            self.main_window.waveform_timeline.set_words_data(self.canvas.words_data)
        
        if hasattr(self.main_window, 'autosave_manager'):
            self.main_window.autosave_manager.schedule(self.main_window._build_autosave_payload)
        if hasattr(self.main_window, '_after_undo_redo'):
            self.main_window._after_undo_redo(action, True)

    def redo(self):
        if not self.redo_stack: return
        action = self.redo_stack.pop()
        self._applying = True
        try:
            undo_action = self._apply_action(action)
        finally:
            self._applying = False
        self.undo_stack.append(undo_action)
        self.canvas.update()
        if hasattr(self.main_window, 'waveform_timeline') and self.main_window.waveform_timeline:
            self.main_window.waveform_timeline.set_words_data(self.canvas.words_data)
        
        if hasattr(self.main_window, 'autosave_manager'):
            self.main_window.autosave_manager.schedule(self.main_window._build_autosave_payload)
        if hasattr(self.main_window, '_after_undo_redo'):
            self.main_window._after_undo_redo(action, False)

    def push_state(self, current, apply, before):
        """Undo for anything outside the transcript words (story board, colour and pause choices):
        current() returns the state now, apply(state) puts a state back, before is the state the
        change replaced."""
        self.push({"type": "state", "current": current, "apply": apply, "value": before, "changes": {"_": 1}})

    def _apply_action(self, action):
        if action.get('type') == 'state':
            now = action['current']()
            action['apply'](action['value'])
            return dict(action, value=now)
        if action.get('type') == 'speakers':
            # {"spk": {index: speaker or None}, "meta": speaker list}: swap with the current state
            import speakers_model
            words = self.canvas.words_data
            back = {i: words[i].get('spk') for i in action['spk'] if 0 <= i < len(words)}
            back_meta = speakers_model.get(words)
            speakers_model.restore(words, list(action['spk'].items()))
            speakers_model.put(words, action['meta'])
            if hasattr(self.main_window, '_speakers_changed'):
                self.main_window._speakers_changed(push_undo=False)
            return {"type": "speakers", "spk": back, "meta": back_meta, "changes": {"_": 1}}
        if action.get('type') == 'words':
            current = self.canvas.words_data
            self.canvas.load_data(action['words'])
            return {"type": "words", "words": current, "changes": {"_": 1}}
        reverse_changes = {}
        id_map = {wo['id']: wo for wo in self.canvas.words_data}
        layer_engine = getattr(self.main_window, '_calculate_visual_layer', None)

        for wid, state in action['changes'].items():
            word_obj = id_map.get(wid)
            if not word_obj: continue

            # Save current state for reverse action
            reverse_changes[wid] = {
                'status': word_obj.get('status'),
                'manual_status': word_obj.get('manual_status'),
                'algo_status': word_obj.get('algo_status'),
                'is_auto': word_obj.get('is_auto'),
                'selected': word_obj.get('selected'),
                'overlay_suppressed': word_obj.get('overlay_suppressed', False)
            }

            # Apply restored state
            word_obj['status'] = state.get('status')
            word_obj['manual_status'] = state.get('manual_status')
            if 'algo_status' in state:
                word_obj['algo_status'] = state.get('algo_status')
            word_obj['is_auto'] = state.get('is_auto')
            word_obj['selected'] = state.get('selected')
            word_obj['overlay_suppressed'] = state.get('overlay_suppressed', True)

            if layer_engine:
                layer_engine(word_obj)

        return {"type": "paint", "changes": reverse_changes}


