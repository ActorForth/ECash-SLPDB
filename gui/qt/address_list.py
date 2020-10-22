#!/usr/bin/env python3
#
# Electrum - lightweight Bitcoin client
# Copyright (C) 2015 Thomas Voegtlin
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation files
# (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge,
# publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS
# BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
# ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

from functools import partial

from .util import MyTreeWidget, MONOSPACE_FONT, SortableTreeWidgetItem, rate_limited, webopen
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QFont, QColor, QKeySequence
from PyQt5.QtWidgets import QTreeWidgetItem, QAbstractItemView, QMenu
from electroncash.i18n import _
from electroncash.address import Address, Script, hash160
from electroncash.plugins import run_hook
import electroncash.web as web
from electroncash.util import profiler
from electroncash import networks


class AddressList(MyTreeWidget):
    filter_columns = [0, 1, 2]  # Address, Label, Balance, ?SLP Vault?
    def __init__(self, parent=None):
        super().__init__(parent, self.create_menu, [], 2, deferred_updates=True)
        self.wallet = self.parent.wallet
        self.refresh_headers()
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setSortingEnabled(True)
        # force attributes to always be defined, even if None, at construction.

    def filter(self, p):
        ''' Reimplementation from superclass filter.  Chops off the
        "bitcoincash:" prefix so that address filters ignore this prefix.
        Closes #1440. Modified by Calin to also handle "simpleledger:". '''
        cashaddr_prefix = f"{networks.net.CASHADDR_PREFIX}:".lower()
        slpaddr_prefix = f"{networks.net.SLPADDR_PREFIX}:".lower()
        p = p.strip()
        if len(p) > len(cashaddr_prefix) and p.lower().startswith(cashaddr_prefix):
            p = p[len(cashaddr_prefix):]  # chop off prefix
        elif len(p) > len(slpaddr_prefix) and p.lower().startswith(slpaddr_prefix):
            p = p[len(slpaddr_prefix):]  # chop off SLP prefix, if any
        super().filter(p)  # call super on chopped-off-piece

    def refresh_headers(self):
        if self.wallet.wallet_type == 'slp_standard':
            headers = [ _('Address'), _('Index'),_('Label'), _('Balance'), _('Tx'), _('Vault Tx'), _('Vault Status')]
        else:
            headers = [ _('Address'), _('Index'),_('Label'), _('Balance'), _('Tx')]
        fx = self.parent.fx
        if fx and fx.get_fiat_address_config():
            headers.insert(4, '{} {}'.format(fx.get_currency(), _('Balance')))
        self.update_headers(headers)

    @rate_limited(1.0, ts_after=True) # We rate limit the address list refresh no more than once every second
    def update(self):
        if self.wallet and (not self.wallet.thread or not self.wallet.thread.isRunning()):
            # short-cut return if window was closed and wallet is stopped
            return
        super().update()

    @profiler
    def on_update(self):
        def item_path(item): # Recursively builds the path for an item eg 'parent_name/item_name'
            return item.text(0) if not item.parent() else item_path(item.parent()) + "/" + item.text(0)
        def remember_expanded_items(root):
            # Save the set of expanded items... so that address list updates don't annoyingly collapse
            # our tree list widget due to the update. This function recurses. Pass self.invisibleRootItem().
            expanded_item_names = set()
            for i in range(0, root.childCount()):
                it = root.child(i)
                if it and it.childCount():
                    if it.isExpanded():
                        expanded_item_names.add(item_path(it))
                    expanded_item_names |= remember_expanded_items(it) # recurse
            return expanded_item_names
        def restore_expanded_items(root, expanded_item_names):
            # Recursively restore the expanded state saved previously. Pass self.invisibleRootItem().
            for i in range(0, root.childCount()):
                it = root.child(i)
                if it and it.childCount():
                    restore_expanded_items(it, expanded_item_names) # recurse, do leaves first
                    old = bool(it.isExpanded())
                    new = bool(item_path(it) in expanded_item_names)
                    if old != new:
                        it.setExpanded(new)
        self.wallet = self.parent.wallet
        had_item_count = self.topLevelItemCount()
        sels = self.selectedItems()
        addresses_to_re_select = {item.data(0, Qt.UserRole) for item in sels}
        expanded_item_names = remember_expanded_items(self.invisibleRootItem())
        del sels  # avoid keeping reference to about-to-be delete C++ objects
        self.clear()
        # Note we take a shallow list-copy because we want to avoid
        # race conditions with the wallet while iterating here. The wallet may
        # touch/grow the returned lists at any time if a history comes (it
        # basically returns a reference to its own internal lists). The wallet
        # may then, in another thread such as the Synchronizer thread, grow
        # the receiving or change addresses on Deterministic wallets.  While
        # probably safe in a language like Python -- and especially since
        # the lists only grow at the end, we want to avoid bad habits.
        # The performance cost of the shallow copy below is negligible for 10k+
        # addresses even on huge wallets because, I suspect, internally CPython
        # does this type of operation extremely cheaply (probably returning
        # some copy-on-write-semantics handle to the same list).
        receiving_addresses = list(self.wallet.get_receiving_addresses())
        change_addresses = list(self.wallet.get_change_addresses())

        if self.parent.fx and self.parent.fx.get_fiat_address_config():
            fx = self.parent.fx
        else:
            fx = None
        account_item = self
        sequences = [0,1] if change_addresses else [0]
        items_to_re_select = []
        for is_change in sequences:
            if len(sequences) > 1:
                name = _("Receiving") if not is_change else _("Change")
                seq_item = QTreeWidgetItem( [ name, '', '', '', '', ''] )
                account_item.addChild(seq_item)
                if not had_item_count: # first time we create this widget, auto-expand the default address list
                    seq_item.setExpanded(True)
                    expanded_item_names.add(item_path(seq_item))
            else:
                seq_item = account_item
            hidden_item = QTreeWidgetItem( [ _("Empty") if is_change else _("Used"), '', '', '', '', ''] )
            has_hidden = False
            addr_list = change_addresses if is_change else receiving_addresses
            for n, address in enumerate(addr_list):
                num = len(self.wallet.get_address_history(address))
                if is_change:
                    is_hidden = self.wallet.is_empty(address)
                else:
                    is_hidden = self.wallet.is_used(address)
                balance = sum(self.wallet.get_addr_balance(address))
                address_text = address.to_ui_string()
                label = self.wallet.labels.get(address.to_storage_string(), '')
                balance_text = self.parent.format_amount(balance, whitespaces=True)
                if self.wallet.wallet_type == 'slp_standard':
                    slp_vault_addr = address.get_slp_vault()
                    slp_vault_tx_count = len(self.wallet.get_address_history(slp_vault_addr))
                    slp_vault_coin_count = len(self.wallet.get_spendable_coins([slp_vault_addr], self.parent.config))
                    if slp_vault_tx_count > 0:
                        columns = [ address_text, str(n), label, balance_text, str(num), str(slp_vault_tx_count) if slp_vault_tx_count else '', 'Sweep me!' if slp_vault_coin_count > 0 else 'All clean.' ]
                    else:
                        columns = [ address_text, str(n), label, balance_text, str(num), '', '' ]
                else:
                    columns = [ address_text, str(n), label, balance_text, str(num) ]
                if fx:
                    rate = fx.exchange_rate()
                    fiat_balance = fx.value_str(balance, rate)
                    columns.insert(4, fiat_balance)
                address_item = SortableTreeWidgetItem(columns)
                address_item.setTextAlignment(3, Qt.AlignRight)
                address_item.setFont(3, QFont(MONOSPACE_FONT))
                if fx:
                    address_item.setTextAlignment(4, Qt.AlignRight)
                    address_item.setFont(4, QFont(MONOSPACE_FONT))

                address_item.setFont(0, QFont(MONOSPACE_FONT))
                address_item.setData(0, Qt.UserRole, address)
                address_item.setData(0, Qt.UserRole+1, True) # label can be edited
                if self.wallet.is_frozen(address):
                    address_item.setBackground(0, QColor('lightblue'))
                if self.wallet.is_beyond_limit(address, is_change):
                    address_item.setBackground(0, QColor('red'))
                if is_hidden:
                    if not has_hidden:
                        seq_item.insertChild(0, hidden_item)
                        has_hidden = True
                    hidden_item.addChild(address_item)
                else:
                    seq_item.addChild(address_item)
                if address in addresses_to_re_select:
                    items_to_re_select.append(address_item)

        for item in items_to_re_select:
            # NB: Need to select the item at the end because internally Qt does some index magic
            # to pick out the selected item and the above code mutates the TreeList, invalidating indices
            # and other craziness, which might produce UI glitches. See #1042
            item.setSelected(True)

        # Now, at the very end, enforce previous UI state with respect to what was expanded or not. See #1042
        restore_expanded_items(self.invisibleRootItem(), expanded_item_names)

    def create_menu(self, position):
        from electroncash.wallet import Multisig_Wallet, Slp_Vault_Wallet
        is_multisig = isinstance(self.wallet, Multisig_Wallet)
        is_slp_vault = isinstance(self.wallet, Slp_Vault_Wallet)
        can_delete = self.wallet.can_delete_address()
        selected = self.selectedItems()
        multi_select = len(selected) > 1
        addrs = [item.data(0, Qt.UserRole) for item in selected]
        if not addrs:
            return
        addrs = [addr for addr in addrs if isinstance(addr, Address)]

        menu = QMenu()

        def doCopy(txt):
            txt = txt.strip()
            self.parent.copy_to_clipboard(txt)

        col = self.currentColumn()
        column_title = self.headerItem().text(col)

        if not multi_select:
            item = self.itemAt(position)
            if not item:
                return
            if not addrs:
                item.setExpanded(not item.isExpanded())
                return
            addr = addrs[0]

            alt_copy_text, alt_column_title = None, None
            if col == 0:
                copy_text = addr.to_full_ui_string()
                if Address.FMT_UI == Address.FMT_LEGACY:
                    alt_copy_text, alt_column_title = addr.to_full_string(Address.FMT_CASHADDR), _('Cash Address')
                else:
                    alt_copy_text, alt_column_title = addr.to_full_string(Address.FMT_LEGACY), _('Legacy Address')
            else:
                copy_text = item.text(col)
            if len(self.wallet.get_address_history(addr.get_slp_vault())):
                menu.addAction("Sweep SLP Vault", lambda: self.parent.sweep_slp_vault(addr.hash160))
                menu.addSeparator()
            menu.addAction(_("Copy {}").format(column_title), lambda: doCopy(copy_text))
            if alt_copy_text and alt_column_title:
                # Add 'Copy Legacy Address' and 'Copy Cash Address' alternates if right-click is on column 0
                menu.addAction(_("Copy {}").format(alt_column_title), lambda: doCopy(alt_copy_text))
            menu.addAction(_('Details'), lambda: self.parent.show_address(addr))
            if col in self.editable_columns:
                menu.addAction(_("Edit {}").format(column_title), lambda: self.editItem(self.itemAt(position), # NB: C++ item may go away if this widget is refreshed while menu is up -- so need to re-grab and not store in lamba. See #953
                                                                                        col))
            a = menu.addAction(_("Request payment"), lambda: self.parent.receive_at(addr))
            if self.wallet.get_num_tx(addr) or self.wallet.has_payment_request(addr):
                # This address cannot be used for a payment request because
                # the receive tab will refuse to display it and will instead
                # create a request with a new address, if we were to call
                # self.parent.receive_at(addr). This is because the receive tab
                # now strongly enforces no-address-reuse. See #1552.
                a.setDisabled(True)
            if self.wallet.can_export():
                menu.addAction(_("Private key"), lambda: self.parent.show_private_key(addr))
            if not (is_multisig or is_slp_vault) and not self.wallet.is_watching_only():
                menu.addAction(_("Sign/verify message"), lambda: self.parent.sign_verify_message(addr))
                menu.addAction(_("Encrypt/decrypt message"), lambda: self.parent.encrypt_message(addr))
            if can_delete:
                menu.addAction(_("Remove from wallet"), lambda: self.parent.remove_address(addr))
            addr_URL = web.BE_URL(self.config, 'addr', addr)
            if addr_URL:
                menu.addAction(_("View on block explorer"), lambda: webopen(addr_URL))
        else:
            # multi-select
            if col > -1:
                texts, alt_copy, alt_copy_text = None, None, None
                if col == 0: # address column
                    texts = [a.to_ui_string() for a in addrs]
                    # Add additional copy option: "Address, Balance (n)"
                    alt_copy = _("Copy {}").format(_("Address") + ", " + _("Balance")) + f" ({len(addrs)})"
                    alt_copy_text = "\n".join([a.to_ui_string() + ", " + self.parent.format_amount(sum(self.wallet.get_addr_balance(a)))
                                              for a in addrs])
                else:
                    texts = [i.text(col).strip() for i in selected]
                    texts = [t for t in texts if t]  # omit empty items
                if texts:
                    copy_text = '\n'.join(texts)
                    menu.addAction(_("Copy {}").format(column_title) + f" ({len(texts)})", lambda: doCopy(copy_text))
                if alt_copy and alt_copy_text:
                    menu.addAction(alt_copy, lambda: doCopy(alt_copy_text))

        freeze = self.parent.set_frozen_state
        if any(self.wallet.is_frozen(addr) for addr in addrs):
            menu.addAction(_("Unfreeze"), partial(freeze, addrs, False))
        if not all(self.wallet.is_frozen(addr) for addr in addrs):
            menu.addAction(_("Freeze"), partial(freeze, addrs, True))

        coins = self.wallet.get_spendable_coins(domain = addrs, config = self.config)
        if coins:
            menu.addAction(_("Spend from"),
                           partial(self.parent.spend_coins, coins))

        run_hook('receive_menu', menu, addrs, self.wallet)
        menu.exec_(self.viewport().mapToGlobal(position))

    def keyPressEvent(self, event):
        if event.matches(QKeySequence.Copy) and self.currentColumn() == 0:
            addrs = [i.data(0, Qt.UserRole) for i in self.selectedItems()]
            if addrs and isinstance(addrs[0], Address):
                text = addrs[0].to_full_ui_string()
                self.parent.app.clipboard().setText(text)
        else:
            super().keyPressEvent(event)

    def update_labels(self):
        if self.should_defer_update_incr():
            return
        def update_recurse(root):
            child_count = root.childCount()
            for i in range(child_count):
                item = root.child(i)
                addr = item.data(0, Qt.UserRole)
                if isinstance(addr, Address):
                    label = self.wallet.labels.get(addr.to_storage_string(), '')
                    item.setText(2, label)
                if item.childCount():
                    update_recurse(item)
        update_recurse(self.invisibleRootItem())
