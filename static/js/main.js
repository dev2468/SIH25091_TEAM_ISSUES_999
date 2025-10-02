// main.js
document.addEventListener('DOMContentLoaded', function() {
    // --- Global State ---
    window.allData = {
        timetables: {},
        teacherSubjects: {},
        unassignedLectures: {}
    };

    const TIME_SLOT_LABELS = [
        "08:00-09:00", "09:00-10:00", "10:00-11:00", "11:00-12:00", "12:00-13:00",
        "13:00-14:00", "14:00-15:00", "15:00-16:00", "16:00-17:00", "17:00-18:00"
    ];

    // --- DOM Element References ---
    const fileInput = document.getElementById('scheduleFile');
    const generateBtn = document.getElementById('generateBtn');
    const timetableContainer = document.getElementById('timetable-container');
    const selectorContainer = document.getElementById('timetable-selector-container');
    const timetableSelector = document.getElementById('timetableSelector');
    const facultyListContainer = document.querySelector('.faculty-list-container');
    const subjectListContainer = document.querySelector('.subject-list-container');
    const unassignedListContainer = document.querySelector('.unassigned-list-container');
    const tabs = document.querySelectorAll('.tablink');
    let processedFileName = null;

    // Safety checks
    if (!fileInput || !generateBtn || !timetableContainer || !timetableSelector) {
        console.warn('Some required DOM elements were not found. Make sure your HTML contains the expected elements.');
    }

    // --- Event Listeners ---
    fileInput && fileInput.addEventListener('change', handleFileUpload);
    generateBtn && generateBtn.addEventListener('click', handleGenerateTimetable);
    timetableSelector && timetableSelector.addEventListener('change', handleDivisionChange);
    tabs && tabs.forEach(tab => tab.addEventListener('click', handleTabClick));

    // -------------------------
    // Helper: normalize keys in JS (attempts to match backend normalization)
    // -------------------------
    function normalizeKeyJS(name) {
        if (!name && name !== '') return '';
        // Trim, replace underscores and dashes with spaces, collapse multiple spaces, trim, then Title Case
        const cleaned = String(name).trim().replace(/[_-]+/g, ' ').replace(/\s+/g, ' ');
        // Keep it as original-case and also produce a Title Case form for better matching
        return {
            raw: cleaned,
            title: cleaned.split(' ').map(w => w.charAt(0).toUpperCase() + w.slice(1).toLowerCase()).join(' ')
        };
    }

    // Try a set of candidate keys for lookup (exact, raw, title)
    function findSubjectsForDivision(teacherSubjectsObj, divisionName) {
        if (!teacherSubjectsObj || Object.keys(teacherSubjectsObj).length === 0) return null;
        const candidates = [];
        if (divisionName) {
            const norm = normalizeKeyJS(divisionName);
            candidates.push(divisionName);
            candidates.push(norm.raw);
            candidates.push(norm.title);
            candidates.push(divisionName.replace(/_/g, ' '));
            candidates.push(divisionName.replace(/-/g, ' '));
            candidates.push(divisionName.toLowerCase());
        }

        // Also add keys from teacherSubjectsObj (so we can try matching loosely)
        const keys = Object.keys(teacherSubjectsObj);
        const lowerMap = {};
        keys.forEach(k => lowerMap[k.toLowerCase()] = k);

        // try candidates in order
        for (let c of candidates) {
            if (c === undefined || c === null) continue;
            if (teacherSubjectsObj.hasOwnProperty(c)) {
                console.debug(`Found teacherSubjects under exact key "${c}"`);
                return teacherSubjectsObj[c];
            }
            const low = String(c).toLowerCase();
            if (lowerMap[low]) {
                console.debug(`Found teacherSubjects under case-insensitive key "${lowerMap[low]}" for candidate "${c}"`);
                return teacherSubjectsObj[lowerMap[low]];
            }
        }

        // fallback: if teacherSubjectsObj appears to be already the mapping for the division (no per-division keys)
        // Example: server returned teacherSubjects for only one division directly (no outer keys)
        // Heuristic: if teacherSubjectsObj's values are arrays of pairs or array of strings, assume it IS the list
        const vals = Object.values(teacherSubjectsObj);
        if (vals.length > 0) {
            const sample = vals[0];
            // if sample is array-of-pairs or mapping of teachers then teacherSubjectsObj likely already holds teachers
            if (Array.isArray(teacherSubjectsObj) || Array.isArray(sample) || (typeof sample === 'object' && !Array.isArray(sample))) {
                // However this branch is risky; only return null, but we will attempt below to handle various shapes.
            }
        }

        // Not found
        console.debug('No matching teacherSubjects key found for division. Available keys:', keys);
        return null;
    }

    // -------------------------
    // Upload / Generate
    // -------------------------
    function handleFileUpload() {
        const file = fileInput.files[0]; if (!file) return;
        const formData = new FormData(); formData.append('file', file);
        fetch('/upload', { method: 'POST', body: formData })
            .then(res => res.json())
            .then(data => {
                if (data.error) { alert('Error: ' + data.error); } 
                else {
                    document.getElementById('file-summary').innerHTML = `✅ ${data.summary.Courses} sessions loaded.`;
                    document.getElementById('file-label-text').textContent = file.name;
                    generateBtn.disabled = false; processedFileName = data.processed_file;
                }
            }).catch(err => console.error(err));
    }

    function handleGenerateTimetable() {
        if (!processedFileName) { alert('Please upload a file first.'); return; }
        showLoading(true);
        fetch('/generate', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ processed_file: processedFileName })
        })
        .then(response => response.json())
        .then(data => {
            showLoading(false);
            if (data.error) { alert('Error: ' + data.error); return; }

            // Store all data globally
            window.allData = {
                timetables: data.timetables || {},
                teacherSubjects: data.teacherSubjects || {},
                unassignedLectures: data.unassignedLectures || {}
            };
            
            console.log("✅ Data received from server (top-level keys):", {
                timetablesKeys: Object.keys(window.allData.timetables),
                teacherSubjectsKeys: Object.keys(window.allData.teacherSubjects),
                unassignedKeys: Object.keys(window.allData.unassignedLectures)
            });
            console.debug('Raw teacherSubjects object:', window.allData.teacherSubjects);

            const divisionNames = Object.keys(window.allData.timetables);
            if (divisionNames.length > 0) {
                populateSelector(divisionNames.sort());
                const firstDivision = divisionNames[0];
                // select it in the dropdown so events and UI reflect the choice
                timetableSelector.value = firstDivision;
                updateAllViews(firstDivision);
                renderFacultyLoad(data.facultyLoad);
                document.getElementById('exportBtn').style.display = 'block';
            } else {
                timetableContainer.innerHTML = '<div class="placeholder"><h2>No timetables generated.</h2></div>';
            }
        })
        .catch(error => { showLoading(false); console.error('Error:', error); });
    }

    function handleDivisionChange() {
        updateAllViews(this.value);
    }

    // -------------------------
    // Update views for a division
    // -------------------------
    function updateAllViews(divisionName) {
        console.log(`🔄 Updating views for division: "${divisionName}"`);

        // find subjects using multiple fallbacks
        const subjectsCandidate = findSubjectsForDivision(window.allData.teacherSubjects, divisionName);

        // If findSubjectsForDivision returned null, there are two more possible shapes:
        // 1) window.allData.teacherSubjects itself is an array of [division, pairs] or mapping containing divisions as nested dicts.
        //    We'll attempt to check if teacherSubjects has a nested structure like { "Division": [ [teacher,subject], ... ] }
        let subjectsForDivision = subjectsCandidate;

        if (!subjectsForDivision) {
            // If teacherSubjects has top-level divisions, try direct property access (already attempted above)
            // If teacherSubjects contains entries like: { "B.Tech (AI)-III-A": [ [teacher,subject], ... ] } we've already checked.
            // Another shape: { "B.Tech (AI)-III-A": { "Teacher": [subjects] } } -> already checked above too.
            // Fallback: If teacherSubjects appears to be an object where the first key IS the division (rare), try to detect it
            const tsKeys = Object.keys(window.allData.teacherSubjects);
            if (tsKeys.length === 0) {
                subjectsForDivision = null;
            } else {
                // If the values are arrays and none matched divisionName, maybe window.allData.teacherSubjects is actually already the mapping for the current division
                // (i.e., backend returned only the mapping for one division directly instead of a container)
                // Heuristic: if all keys look like teacher names (contain spaces and no days), assume it's already the mapping.
                const looksLikeTeacherMapping = tsKeys.every(k => typeof k === 'string' && k.split(' ').length <= 4 && k.indexOf('Monday') === -1);
                if (looksLikeTeacherMapping) {
                    console.debug('Heuristic: teacherSubjects top-level keys look like teacher names; treating teacherSubjects as the mapping for current division.');
                    subjectsForDivision = window.allData.teacherSubjects;
                }
            }
        }

        // Final safety: if still null, set to empty
        if (!subjectsForDivision) {
            console.warn('No subject data found for division:', divisionName);
            subjectsForDivision = [];
        }

        const unassignedForDivision = window.allData.unassignedLectures[divisionName] || [];

        console.debug('Subjects to render (after normalization/fallbacks):', subjectsForDivision);

        renderTimetable(window.allData.timetables[divisionName]);
        renderTeacherSubjects(subjectsForDivision);
        renderUnassigned(unassignedForDivision);
    }

    // -------------------------
    // Renderers
    // -------------------------
    function populateSelector(divisionNames) {
        if (!timetableSelector) return;
        timetableSelector.innerHTML = divisionNames.map(name => `<option value="${escapeHtml(name)}">${escapeHtml(name)}</option>`).join('');
        selectorContainer && (selectorContainer.style.display = 'flex');
    }

    function renderTimetable(divisionTimetable) {
        if (!divisionTimetable) { timetableContainer.innerHTML = '<div class="placeholder"><h2>No timetable for this division.</h2></div>'; return; }
        const days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
        let html = '<table class="timetable-grid"><thead><tr><th class="time-slot-header">Time</th>';
        days.forEach(day => html += `<th>${day}</th>`); html += '</tr></thead><tbody>';
        TIME_SLOT_LABELS.forEach((slotLabel, slotIndex) => {
            html += `<tr><td class="time-slot-header">${slotLabel}</td>`;
            days.forEach(day => {
                const session = (divisionTimetable[day] || [])[slotIndex];
                html += '<td>';
                if (session) {
                    const typeClass = (session.type || '').toString().toLowerCase().includes('practical') ? 'lab' : ((session.type || '').toString().toLowerCase().includes('tutorial') ? 'tutorial' : 'theory');
                    html += `<div class="session-card type-${typeClass}" draggable="true">
                                <span class="session-course">${escapeHtml(session.subject || '')}</span>
                                <span class="session-faculty">${escapeHtml(session.faculty || '')}</span>
                             </div>`;
                }
                html += '</td>';
            });
            html += '</tr>';
        });
        html += '</tbody></table>';
        timetableContainer.innerHTML = html;
    }

    function renderFacultyLoad(facultyLoad) {
        if (!facultyListContainer) return;
        const sortedFaculty = Object.entries(facultyLoad || {}).sort((a, b) => a[0].localeCompare(b[0]));
        if (sortedFaculty.length === 0) {
            facultyListContainer.innerHTML = '<p>No faculty data.</p>';
            return;
        }
        facultyListContainer.innerHTML = sortedFaculty.map(([name, data]) => `
            <div class="faculty-item">
                <span class="faculty-name">${escapeHtml(name)}</span>
                <span class="faculty-details"><strong>${data.hours}</strong> hours/week | Courses: ${data.courses.length}</span>
            </div>`).join('');
    }

    // Accepts either:
    //  - Array of [teacher, subject] pairs
    //  - Mapping { teacher: [subjects] }
    function renderTeacherSubjects(subjectData) {
        if (!subjectListContainer) {
            console.warn('Subject list container not found in DOM.');
            return;
        }

        // Normalize empty cases
        if (!subjectData || (Array.isArray(subjectData) && subjectData.length === 0) || (typeof subjectData === 'object' && Object.keys(subjectData).length === 0)) {
            subjectListContainer.innerHTML = '<p>No subject assignments for this division.</p>';
            return;
        }

        // If it's an array of pairs: [[teacher, subject], ...]
        let groupedByTeacher = {};
        if (Array.isArray(subjectData)) {
            // Might be array of pairs or array of objects; handle pairs
            subjectData.forEach(item => {
                if (Array.isArray(item) && item.length >= 2) {
                    const teacher = String(item[0]).trim();
                    const subject = String(item[1]).trim();
                    if (!groupedByTeacher[teacher]) groupedByTeacher[teacher] = [];
                    groupedByTeacher[teacher].push(subject);
                } else if (typeof item === 'object' && item !== null) {
                    // If item is object, try {teacher: subjectList} or {teacher: subject}
                    Object.entries(item).forEach(([k, v]) => {
                        if (!groupedByTeacher[k]) groupedByTeacher[k] = [];
                        if (Array.isArray(v)) groupedByTeacher[k] = groupedByTeacher[k].concat(v);
                        else groupedByTeacher[k].push(String(v));
                    });
                }
            });
        } else if (typeof subjectData === 'object') {
            // Already mapping { teacher: [subjects] }
            // Ensure all values are arrays
            Object.entries(subjectData).forEach(([teacher, subjects]) => {
                if (Array.isArray(subjects)) groupedByTeacher[teacher] = subjects;
                else if (subjects === null || subjects === undefined) groupedByTeacher[teacher] = [];
                else groupedByTeacher[teacher] = [String(subjects)];
            });
        } else {
            // unknown shape
            console.warn('Unknown shape for subjectData:', subjectData);
            subjectListContainer.innerHTML = '<p>No subject assignments for this division.</p>';
            return;
        }

        // Render
        const entries = Object.entries(groupedByTeacher).sort((a, b) => a[0].localeCompare(b[0]));
        if (entries.length === 0) {
            subjectListContainer.innerHTML = '<p>No subject assignments for this division.</p>';
            return;
        }

        subjectListContainer.innerHTML = entries.map(([teacher, subjects]) => `
            <div class="faculty-item">
                <span class="faculty-name">${escapeHtml(teacher)}</span>
                <ul class="subject-sublist">
                    ${subjects.map(s => `<li>${escapeHtml(s)}</li>`).join('')}
                </ul>
            </div>`).join('');
    }

    function renderUnassigned(unassignedList) {
        if (!unassignedListContainer) {
            console.warn('Unassigned list container not found in DOM.');
            return;
        }
        if (!unassignedList || unassignedList.length === 0) {
            unassignedListContainer.innerHTML = '<p>🎉 All sessions were scheduled successfully!</p>';
            return;
        }
        unassignedListContainer.innerHTML = unassignedList.map(lecture => `
            <div class="unassigned-item">${escapeHtml(lecture)}</div>`).join('');
    }

    // -------------------------
    // Tabs + other utils
    // -------------------------
    function handleTabClick(event) {
        const tabName = event.currentTarget.dataset.tab;
        document.querySelectorAll('.tabcontent').forEach(tc => tc.classList.remove('active'));
        document.querySelectorAll('.tablink').forEach(tl => tl.classList.remove('active'));
        const target = document.getElementById(tabName);
        if (target) target.classList.add('active');
        event.currentTarget.classList.add('active');
    }

    function showLoading(isLoading) {
        const spinner = document.getElementById('loading-spinner');
        if (spinner) spinner.style.display = isLoading ? 'block' : 'none';
        if (generateBtn) generateBtn.style.display = isLoading ? 'none' : 'flex';
    }

    // small helper to avoid XSS from injection when we put strings into innerHTML
    function escapeHtml(unsafe) {
        return String(unsafe === undefined || unsafe === null ? '' : unsafe)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#039;");
    }
});
