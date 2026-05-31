// contacts-app.js — bootstrap for the /contacts page
(function() {
  var listEl = document.getElementById('contacts-list');
  var detailEl = document.getElementById('contacts-detail');

  var listComponent = new AddressBookList(listEl);
  var detailComponent = new ContactDetail(detailEl);

  listComponent.mount();
  detailComponent.mount();

  // Initial render: loading state
  listComponent.render({ loading: true, error: null, searchQuery: '', letterGroups: null, activeContact: null, activeTab: 'contacts' });

  var _groupsLoaded = false;
  var _groupsLoading = false;

  // Load contacts from API with pagination
  async function loadContacts() {
    AddressBookStore.data.loading = true;
    AddressBookStore.data.error = null;
    listComponent._renderState();
    try {
      var params = {
        q: AddressBookStore.data.searchQuery,
        page: AddressBookStore.data.page,
        per_page: AddressBookStore.data.perPage,
      };
      var contactsData = await api.addressBook(params);
      AddressBookStore.data.contacts = contactsData.contacts;
      AddressBookStore.data.total = contactsData.total;
      AddressBookStore.data.page = contactsData.page;
      AddressBookStore.data.totalPages = contactsData.total_pages;
    } catch (e) {
      if (e.name !== 'AbortError') {
        AddressBookStore.data.error = '加载通讯录失败: ' + (e.message || '未知错误');
      }
    }
    AddressBookStore.data.loading = false;
    AddressBookStore.emit('contacts-loaded');
  }

  // Lazy-load groups only when switching to groups tab
  async function loadGroups() {
    if (_groupsLoaded || _groupsLoading) return;
    _groupsLoading = true;
    try {
      var groupsData = await api.addressBookGroups();
      AddressBookStore.data.groups = groupsData.groups || [];
      _groupsLoaded = true;
    } catch (e) {
      if (e.name !== 'AbortError') {
        console.error('Failed to load groups:', e);
      }
    }
    _groupsLoading = false;
    // Re-render to show groups
    AddressBookStore.emit('contacts-loaded');
  }

  // Listen for tab changes to lazy-load groups
  AddressBookStore.on('tab-changed', function(tab) {
    if (tab === 'groups' && !_groupsLoaded) {
      AddressBookStore.data.loading = true;
      listComponent._renderState();
      loadGroups().then(function() {
        AddressBookStore.data.loading = false;
        AddressBookStore.emit('contacts-loaded');
      });
    }
  });

  // Go to specific page
  function goToPage(page) {
    if (page < 1 || page > AddressBookStore.data.totalPages) return;
    AddressBookStore.data.page = page;
    loadContacts();
  }

  // Debounced search: reload from server with query
  var searchInput = document.getElementById('contacts-search');
  if (searchInput) {
    var searchTimer = null;
    searchInput.addEventListener('input', function() {
      clearTimeout(searchTimer);
      var self = this;
      searchTimer = setTimeout(function() {
        AddressBookStore.data.searchQuery = self.value.trim();
        AddressBookStore.data.page = 1;
        loadContacts();
      }, 300);
    });
  }

  // Expose goToPage for onclick handlers in template
  window._contactsGoToPage = goToPage;

  loadContacts();
})();
