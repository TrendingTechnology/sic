# Generated by Django 3.1.3 on 2021-07-06 11:41

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('sic', '0021_alter_invitation_receiver'),
    ]

    operations = [
        migrations.AddField(
            model_name='tag',
            name='parents',
            field=models.ManyToManyField(blank=True, to='sic.Tag'),
        ),
    ]
