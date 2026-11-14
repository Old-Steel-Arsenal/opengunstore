// Marketing Contact list: a coloured status and, until someone picks another view, Subscribed contacts only.
frappe.listview_settings['Marketing Contact'] = {
	add_fields: ['status'],
	filters: [['status', '=', 'Subscribed']],
	get_indicator: function (doc) {
		var colours = {
			'Subscribed': 'green',
			'Not Subscribed': 'gray',
			'Unsubscribed': 'orange',
			'Bounced': 'red',
			'Complained': 'red',
		};
		return [__(doc.status), colours[doc.status] || 'gray', 'status,=,' + doc.status];
	},
};
